"""Hand over from the previous ZIP folder when `./app-build web` starts a new version.

George updates by renaming the old folder to `<name>-old` and unzipping the new version as
`<name>`. Starting the new Web Console therefore:

1. copies `<name>-old/.appbuilder` (tasks, schedules, settings, logs) when this folder has no
   data yet - copy only, the old folder is never changed;
2. stops this tool's Web Console and scheduler processes that run from ANY other folder, so
   an old scheduler cannot keep dispatching with old code. Build workers are never touched;
3. points the optional login LaunchAgent at this folder if it still points at another one.
"""
from __future__ import annotations

import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import DATA, ROOT

SERVICE = re.compile(r'(?:^|\s)-m\s+appbuilder\s+(?:web|scheduler\s+run)(?:\s|$)')


def say(message):
    print('[UPDATE] ' + message, flush=True)


def carry_data(root=ROOT, data=DATA):
    previous = root.parent / (root.name + '-old')
    if (data / 'tasks.sqlite').exists() or not (previous / '.appbuilder').is_dir():
        return None
    if data.exists() and any(data.iterdir()):
        say(f'本文件夹已有部分数据，未自动复制旧版本数据；如需要请运行 python3 -m appbuilder.migrate {previous}')
        return None
    if data.exists():
        data.rmdir()
    from .migrate import import_state
    copied = import_state(previous, root)
    say(f'已从上一个版本 {previous.name} 复制任务、定时任务和设置（旧文件夹保持不变）')
    return copied


def _processes():
    try:
        out = subprocess.run(['ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid.isdigit() and SERVICE.search(command):
            rows.append((int(pid), command.strip()))
    return rows


def _folder(pid, command):
    """The project folder a service runs from: its venv python path, else its working directory."""
    match = re.search(r'(/\S+?)/\.venv/bin/python', command)
    if match:
        return Path(match[1]).resolve()
    try:
        out = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith('n/'):
            return Path(line[1:]).resolve()
    return None


def stop_other_folders(root=ROOT, processes=None, folder_of=None, kill=os.kill, wait=5.0):
    """Stop this tool's web/scheduler processes that belong to another folder."""
    root = Path(root).resolve()
    folder_of = folder_of or _folder
    own = {os.getpid(), os.getppid()}
    targets = []
    for pid, command in (processes if processes is not None else _processes()):
        if pid in own:
            continue
        folder = folder_of(pid, command)
        if folder is not None and folder != root and (folder / 'appbuilder').is_dir():
            targets.append((pid, folder, 'scheduler' if 'scheduler' in command else 'web'))
    for pid, folder, kind in targets:
        try:
            kill(pid, signal.SIGTERM)
            say(f'停止旧版本的{"调度器" if kind == "scheduler" else "网页"}（{folder.name}，进程 {pid}）')
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + wait
    for pid, _, _ in targets:
        while time.monotonic() < deadline:
            try:
                kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return targets


def repoint_launch_agent(root=ROOT):
    """If the login LaunchAgent still runs the scheduler from another folder, reinstall it here."""
    if sys.platform != 'darwin':
        return False
    from .scheduler import install, plist_path
    path = plist_path()
    if not path.exists():
        return False
    try:
        current = Path(plistlib.loads(path.read_bytes()).get('WorkingDirectory', '')).resolve()
    except Exception:
        return False
    if current == Path(root).resolve():
        return False
    install()
    say(f'开机自启的调度器原来指向 {current.name}，已改为当前文件夹')
    return True


def hand_over():
    for step in (carry_data, stop_other_folders, repoint_launch_agent):
        try:
            step()
        except Exception as exc:                     # never block starting the page
            say(f'{step.__name__} 未完成：{type(exc).__name__}: {exc}')
