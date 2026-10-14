"""Project-local admission gate and immediate stop of verified process instances."""
from contextlib import contextmanager
from functools import wraps
import fcntl
import json
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import time
import uuid
import re
from .config import DATA, ROOT

_mutex = threading.RLock()
_local = threading.local()

@contextmanager
def control(data=None):
    directory = Path(data or DATA)
    directory.mkdir(parents=True, exist_ok=True)
    with _mutex:
        key = str(directory.resolve())
        depths = getattr(_local, 'depths', {})
        _local.depths = depths
        if depths.get(key, 0):
            depths[key] += 1
            try: yield
            finally: depths[key] -= 1
            return
        with (directory / 'lifecycle.lock').open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            depths[key] = 1
            try: yield
            finally: depths[key] = 0

def state(data=None):
    try: return json.loads((Path(data or DATA) / 'lifecycle.json').read_text())
    except FileNotFoundError: return {'mode': 'RUNNING'}

def _save(value, data=None):
    directory = Path(data or DATA)
    temporary = directory / 'lifecycle.json.tmp'
    temporary.write_text(json.dumps(value))
    os.replace(temporary, directory / 'lifecycle.json')

def assert_accepting(data=None):
    if state(data)['mode'] != 'RUNNING':
        raise RuntimeError('本项目正在安全停机，暂停接收新操作；用 ./app-build start 恢复')

@contextmanager
def admission(data=None):
    with control(data):
        assert_accepting(data)
        yield

def admitted(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        # store.DATA may be redirected by tests; never touch another project.
        with admission(function.__globals__.get('DATA', DATA)):
            return function(*args, **kwargs)
    return wrapped

def should_exit():
    return state()['mode'] in ('STOPPING', 'STOPPED')

def is_draining():
    return state()['mode'] != 'RUNNING'

@contextmanager
def service(name):
    """Held OS lease, not a PID file: stale files and PID reuse cannot own it."""
    with admission():
        directory = DATA / 'services'
        directory.mkdir(exist_ok=True)
        handle = (directory / f'{name}-{os.getpid()}.lock').open('a+')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try: yield
    finally: handle.close()

def held(path):
    with path.open('a+') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        return False

def reserve_activity():
    directory = DATA / 'activities'
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / (uuid.uuid4().hex + '.lock')).open('a+')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle

def busy_activities():
    return any(held(path) for path in (DATA / 'activities').glob('*.lock'))

def legacy_processes():
    """Read-only inventory. Unknown identity blocks stopping; never signal it."""
    result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True)
    if result.returncode: raise RuntimeError('无法核实本项目后台进程')
    unknown = []
    for line in result.stdout.splitlines():
        match = re.match(r'\s*(\d+)\s+.*?\s-m\s+appbuilder\s+(web|scheduler|worker)(?:\s|$)', line)
        if not match: continue
        pid, kind = int(match[1]), match[2]
        if pid == os.getpid(): continue
        try:
            if sys.platform == 'darwin':
                cwd_result = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True)
                names = [x[1:] for x in cwd_result.stdout.splitlines() if x.startswith('n')]
                if cwd_result.returncode or len(names) != 1: raise RuntimeError('无法核实后台进程工作目录')
                cwd = Path(names[0]).resolve()
            else: cwd = Path(os.readlink(f'/proc/{pid}/cwd')).resolve()
        except FileNotFoundError: continue
        if cwd != ROOT.resolve(): continue
        if kind == 'worker':
            # Its task lease/status is checked independently; do not signal it.
            from .store import task_list
            if not any(row['id'] in line.split() for row in task_list()): unknown.append(pid)
        else:
            lease = DATA / 'services' / f'{kind}-{pid}.lock'
            if not lease.exists() or not held(lease): unknown.append(pid)
    return unknown

def busy_workers():
    from .store import task_list, task_folder
    rows = task_list()
    return [row['id'] for row in rows if row['status'] in ('RUNNING', 'QUEUED')
            or ((task_folder(row['id']) / 'worker.lock').exists()
                and held(task_folder(row['id']) / 'worker.lock'))]

def _launchctl(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)

def launchd_disk_identity():
    from .scheduler import LABEL, plist_path
    path = plist_path()
    try:
        raw = path.read_bytes()
        info = plistlib.loads(raw)
    except (OSError, ValueError) as exc: raise RuntimeError('无法核实 LaunchAgent 文件') from exc
    args = info.get('ProgramArguments', [])
    if (info.get('Label') != LABEL or Path(info.get('WorkingDirectory', '')).resolve() != ROOT.resolve()
        or args[-4:] != ['-m', 'appbuilder', 'scheduler', 'run']):
        raise RuntimeError('LaunchAgent 不属于当前项目')
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'root': str(ROOT.resolve())}

def owned_launchd():
    """Return loaded target only when both disk and loaded configuration match."""
    if sys.platform != 'darwin': return False
    from .scheduler import LABEL
    target = f'gui/{os.getuid()}/{LABEL}'
    loaded = _launchctl('print', target)
    if loaded.returncode: return False
    identity = launchd_disk_identity()
    lines = {line.strip() for line in loaded.stdout.splitlines()}
    if f"path = {identity['path']}" not in lines or f'working directory = {ROOT}' not in lines:
        raise RuntimeError('LaunchAgent 加载配置不一致；不会更改它')
    return target

def disable_launchd():
    target = owned_launchd()
    if not target: return False
    from .scheduler import LABEL
    identity = launchd_disk_identity()
    before = _launchctl('print-disabled', f'gui/{os.getuid()}')
    if before.returncode: raise RuntimeError('无法核实 LaunchAgent 原启用状态')
    match = re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*(true|false)', before.stdout)
    already_disabled = bool(match and match[1] == 'true')
    if not already_disabled:
        _save({**state(), 'launchd_disable_pending': True, 'launchd_identity': identity})
        result = _launchctl('disable', target)
        if result.returncode: raise RuntimeError('无法暂停 LaunchAgent 自动重启')
        _save({**state(), 'launchd_disabled': True, 'launchd_disable_pending': False})
    # disable alone does not unload a currently loaded KeepAlive service.
    result = _launchctl('bootout', target)
    if result.returncode and _launchctl('print', target).returncode == 0:
        raise RuntimeError('无法卸载已核实的 LaunchAgent；停止受阻')
    return not already_disabled

def resume():
    with control():
        previous = state()
        if previous.get('launchd_disabled') or previous.get('launchd_disable_pending') or previous.get('launchd_restore_pending'):
            from .scheduler import LABEL
            target = f'gui/{os.getuid()}/{LABEL}'
            loaded = owned_launchd()
            identity = launchd_disk_identity()
            if identity != previous.get('launchd_identity'):
                raise RuntimeError('LaunchAgent 文件已改变；保留停机状态，不启用其他项目服务')
            result = _launchctl('enable', target)
            if result.returncode: raise RuntimeError('无法恢复 LaunchAgent；保留停机状态')
            # Admission must be open before launchd starts the scheduler.
            _save({**previous, 'mode': 'RUNNING', 'launchd_restore_pending': True})
            if not loaded:
                result = _launchctl('bootstrap', f'gui/{os.getuid()}', identity['path'])
                if result.returncode:
                    _save({**previous, 'mode': 'STOPPED'})
                    raise RuntimeError('恢复 LaunchAgent 启动失败；保留停机状态')
            _save({'mode': 'RUNNING'})
        else: _save({'mode': 'RUNNING'})
    print('已恢复接收操作；可运行 ./app-build web')

def stop(timeout=5):
    with control():
        return _stop(timeout)

def _stop(timeout=5):
    """Close admission first, then stop only verified local process instances."""
    from . import process_identity
    with control():
        previous = state()
        _save({**previous, 'mode': 'STOPPING'})
        records = process_identity.owned(ROOT)
        changed = disable_launchd()
        disabled = previous.get('launchd_disabled', False) or previous.get('launchd_disable_pending', False) or changed
        _save({**state(), 'mode': 'STOPPING', 'launchd_disabled': disabled})
    # No DB completion/status changes: remote Jenkins and unknown receipts survive.
    print(f'已暂停新操作；停止本项目 {len(records)} 个进程，不等待构建完成。', flush=True)
    for record in records: process_identity.terminate(record)
    deadline = time.monotonic() + timeout
    while any(process_identity.same(record) for record in records) and time.monotonic() < deadline:
        # Include children created during shutdown; admission remains closed.
        known = {(record['pid'], tuple(record['birth'])) for record in records}
        for record in process_identity.owned(ROOT):
            if (record['pid'], tuple(record['birth'])) not in known:
                records.append(record)
                process_identity.terminate(record)
        time.sleep(.1)
    for record in records: process_identity.terminate(record, force=True)
    with control():
        _save({**state(), 'mode': 'STOPPED'})
    print('已向核实的本项目进程完成停机；保留配置、历史、state；不取消远端 Jenkins。')
