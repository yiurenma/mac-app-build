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


def heartbeat_status(now=None):
    """Whether a scheduler process has reported in recently (read-only)."""
    now = time.time() if now is None else now
    try:
        data = json.loads(heartbeat_path().read_text(encoding='utf-8'))
        age = now - float(data['time'])
    except (OSError, ValueError, KeyError, TypeError):
        return {'online': False, 'pid': None, 'age_seconds': None}
    return {'online': 0 <= age <= ONLINE_WITHIN_SECONDS, 'pid': data.get('pid'),
            'age_seconds': round(age, 1), 'version': data.get('version')}


class AlreadyRunning(RuntimeError):
    pass


def run_forever(interval=INTERVAL_SECONDS, *, max_loops=None, sleep=time.sleep, start=None):
    """Poll for due schedules until stopped. Exactly one instance may run."""
    DATA.mkdir(parents=True, exist_ok=True)
    with lock_path().open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning('调度器已经在运行（只允许一个实例）') from exc
        stopping = []
        previous = {s: signal.signal(s, lambda *_: stopping.append(True))
                    for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            print(f'[SCHEDULER] 启动 v{__version__} pid={os.getpid()}；每 {interval}s 检查一次到期任务', flush=True)
            loops = 0
            while not stopping:
                try:
                    write_heartbeat()
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
