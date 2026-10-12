"""Stand-alone scheduler process + macOS LaunchAgent management.

The scheduler is intentionally independent of the Web Console: closing the web
page never stops schedules. A LaunchAgent starts it at login, restarts it if it
crashes, and wraps it in ``caffeinate -i`` so the Mac does not idle-sleep.
(Closing the lid or a manual sleep still suspends every process on macOS.)
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import traceback
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import __version__, schedules
from .config import DATA, ROOT

LABEL = 'com.mac-app-builder.scheduler'
INTERVAL_SECONDS = 15
ONLINE_WITHIN_SECONDS = 60
LAUNCHD_PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


def heartbeat_path():
    return DATA / 'scheduler.json'


def lock_path():
    return DATA / 'scheduler.lock'


def write_heartbeat(extra=None):
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {'pid': os.getpid(), 'time': time.time(), 'version': __version__, **(extra or {})}
    temp = heartbeat_path().with_suffix('.json.tmp')
    temp.write_text(json.dumps(payload), encoding='utf-8')
    os.replace(temp, heartbeat_path())


def config_path():
    return str(Path(os.environ.get('APP_BUILDER_CONFIG', ROOT / 'config.yaml')).resolve())


def runtime_locked():
    """Probe only: never trust or signal a PID from a stale heartbeat."""
    if not lock_path().exists():
        return False
    with lock_path().open('rb') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def heartbeat_status(now=None):
    now = time.time() if now is None else now
    result = {'online': False, 'pid': None, 'age_seconds': None,
              'reason': '尚未启动，Web 正在自动检查', 'log_path': str(DATA / 'scheduler.log')}
    try:
        data = json.loads(heartbeat_path().read_text(encoding='utf-8'))
        age = now - float(data['time'])
        result.update(pid=data.get('pid'), age_seconds=round(age, 1), version=data.get('version'))
        locked = runtime_locked()
        alive = isinstance(data.get('pid'), int) and data['pid'] > 0
        if alive:
            try:
                os.kill(data['pid'], 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                pass
        owner_matches = True
        if locked:
            try:
                owner = json.loads(lock_path().read_text())
                owner_matches = owner.get('instance') == data.get('instance') and owner.get('pid') == data.get('pid')
            except (OSError, ValueError, TypeError):
                owner_matches = not data.get('instance')  # legacy schedulers have an empty lock file
        result['online'] = locked and alive and owner_matches and 0 <= age <= ONLINE_WITHIN_SECONDS
        result['reason'] = '运行中' if result['online'] else '进程已退出或心跳失效；Web 将自动重试'
        if locked and data.get('config') and data['config'] != config_path():
            result.update(online=False, reason='已有调度器使用不同配置；请先在原终端停止它，再启动当前 Web')
        elif result['online'] and not data.get('config'):
            result['reason'] = '复用旧版调度器（配置未标记）；建议方便时重启调度器'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not result['online']:
        try:
            startup = json.loads((DATA / 'scheduler-startup.json').read_text())
            if 0 <= now - startup['time'] < ONLINE_WITHIN_SECONDS and startup['config'] == config_path():
                result['startup'] = startup['reason']
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result


def _startup_status(reason):
    path = DATA / 'scheduler-startup.json'
    temp = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps({'time': time.time(), 'config': config_path(), 'reason': reason}))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class SchedulerSupervisor:
    """Web owns this monitor, never the lifetime of the detached scheduler."""
    def __init__(self):
        self.stopped = threading.Event()
        self.children = []
        self.thread = threading.Thread(target=self._monitor, daemon=True, name='scheduler-monitor')

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=2)

    def _monitor(self):
        while not self.stopped.is_set():
            try:
                self.ensure_running()
            except Exception as exc:
                from .telemetry import safe_text
                reason = '自动启动失败：' + safe_text(str(exc))
                print('[SCHEDULER] ' + reason, flush=True)
                try:
                    _startup_status(reason)
                except OSError:
                    pass
            if self.stopped.wait(INTERVAL_SECONDS):
                break

    def ensure_running(self):
        self.children[:] = [p for p in self.children if p.poll() is None]
        DATA.mkdir(parents=True, exist_ok=True)
        with (DATA / 'scheduler-start.lock').open('a+b') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # another Web is performing the same bounded startup
            status = heartbeat_status()
            if status['online']:
                return
            if runtime_locked():
                _startup_status(status['reason'] + '；运行锁仍被占用，不会重复启动或终止现有进程')
                return
            if self.children:
                _startup_status('后台进程仍在启动但尚无有效心跳，请检查 scheduler.log')
                return
            env = dict(os.environ, APP_BUILDER_CONFIG=config_path(), PYTHONUNBUFFERED='1')
            command = [sys.executable, '-m', 'appbuilder', 'scheduler', 'run']
            _startup_status('正在自动启动后台调度器')
            with (DATA / 'scheduler.log').open('ab') as log:
                child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.children.append(child)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if heartbeat_status()['online']:
                    (DATA / 'scheduler-startup.json').unlink(missing_ok=True)
                    return
                if child.poll() is not None:
                    raise RuntimeError(f'调度器退出（代码 {child.returncode}），请检查 {DATA / "scheduler.log"}')
                if self.stopped.wait(0.1):
                    return
            _startup_status('启动后未收到有效心跳，请检查 scheduler.log；Web 会继续检查，不会重复启动持锁进程')


class AlreadyRunning(RuntimeError):
    pass


def run_forever(interval=INTERVAL_SECONDS, *, max_loops=None, sleep=time.sleep, start=None):
    """Poll for due schedules until stopped. Exactly one instance may run."""
    DATA.mkdir(parents=True, exist_ok=True)
    with lock_path().open('a+b') as handle:
        for attempt in range(3):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if attempt == 2:
                    raise AlreadyRunning('调度器已经在运行（只允许一个实例）') from exc
                time.sleep(0.02)  # a read-only health probe may briefly hold a shared lock
        metadata = {'config': config_path(), 'root': str(ROOT.resolve()), 'data': str(DATA.resolve()),
                    'instance': uuid.uuid4().hex, 'pid': os.getpid()}
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata).encode())
        handle.flush()
        stopping = []
        previous = {s: signal.signal(s, lambda *_: stopping.append(True))
                    for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            print(f'[SCHEDULER] 启动 v{__version__} pid={os.getpid()}；每 {interval}s 检查一次到期任务', flush=True)
            schedules.recover_startup()
            loops = 0
            while not stopping:
                try:
                    write_heartbeat(metadata)
                    result = schedules.tick(start=start)
                    if result['queued'] or result['started']:
                        print(time.strftime('%Y-%m-%d %H:%M:%S'),
                              f"[SCHEDULER] 新入队 {result['queued']}，已启动 {result['started']}", flush=True)
                except Exception:  # noqa: BLE001 - keep the daemon alive, keep the traceback
                    print(time.strftime('%Y-%m-%d %H:%M:%S'), '[SCHEDULER] 本轮检查失败，下一轮重试：', flush=True)
                    traceback.print_exc()
                loops += 1
                if max_loops is not None and loops >= max_loops:
                    break
                sleep(interval)
            print('[SCHEDULER] 已停止', flush=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


# ---- macOS LaunchAgent -------------------------------------------------------

def plist_path():
    return Path.home() / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'


def build_plist(python=None, root=None, data=None):
    python = python or sys.executable
    root = Path(root or ROOT)
    data = Path(data or DATA)
    arguments = [str(python), '-m', 'appbuilder', 'scheduler', 'run']
    caffeinate = shutil.which('caffeinate') or '/usr/bin/caffeinate'
    if os.path.exists(caffeinate):
        arguments = [caffeinate, '-i', *arguments]
    environment = {'PATH': LAUNCHD_PATH, 'PYTHONUNBUFFERED': '1'}
    if os.environ.get('APP_BUILDER_CONFIG'):
        environment['APP_BUILDER_CONFIG'] = os.environ['APP_BUILDER_CONFIG']
    return {
        'Label': LABEL,
        'ProgramArguments': arguments,
        'WorkingDirectory': str(root),
        'EnvironmentVariables': environment,
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'StandardOutPath': str(data / 'scheduler.log'),
        'StandardErrorPath': str(data / 'scheduler.log'),
    }


def _launchctl(*args):
    return subprocess.run(['launchctl', *args], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)


def install():
    if sys.platform != 'darwin':
        raise RuntimeError('LaunchAgent 仅支持 macOS；其他系统请直接运行 ./app-build scheduler run')
    DATA.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as f:
        plistlib.dump(build_plist(), f)
    domain = f'gui/{os.getuid()}'
    _launchctl('bootout', f'{domain}/{LABEL}')  # fine if it was not loaded
    result = _launchctl('bootstrap', domain, str(path))
    if result.returncode != 0:
        raise RuntimeError('launchctl bootstrap 失败：' + result.stdout.strip())
    _launchctl('kickstart', '-k', f'{domain}/{LABEL}')
    return path


def uninstall():
    if sys.platform != 'darwin':
        raise RuntimeError('LaunchAgent 仅支持 macOS')
    _launchctl('bootout', f'gui/{os.getuid()}/{LABEL}')
    path = plist_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return existed
