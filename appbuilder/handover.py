"""Hand over from the previous ZIP folder when `./app-build web` starts a new version.

George updates by renaming the old folder to `<name>-old` and unzipping the new version as
`<name>`. Starting the new Web Console therefore:

1. stops this tool's Web Console and scheduler processes that run from ANY other folder, so
   an old scheduler cannot keep dispatching with old code. Build workers are never touched.
   This comes first: an old process still running from the renamed folder keeps the old
   PATH, so it writes into THIS folder's .appbuilder (seen in practice: an empty
   tasks.sqlite appeared here and blocked the copy);
2. copies `<name>-old/.appbuilder` (tasks, schedules, settings, logs) when this folder has no
   real data yet - copy only, the old folder is never changed. Stray files written here by
   an old process (no tasks at all) are moved aside, not deleted;
3. points the optional login LaunchAgent at this folder if it still points at another one.
"""
from __future__ import annotations

import os
import plistlib
import re
import signal
import subprocess
import sys
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .config import DATA, ROOT

SERVICE = re.compile(r'(?:^|\s)-m\s+appbuilder\s+(?:web|scheduler\s+run)(?:\s|$)')


def say(message):
    print('[UPDATE] ' + message, flush=True)


def _task_count(data):
    """Number of build tasks in a data folder; 0 when there is no usable task database."""
    path = Path(data) / 'tasks.sqlite'
    if not path.exists():
        return 0
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            return db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error:
        return 0


def previous_folder(root=ROOT):
    """The previous version: <name>-old, or when macOS named it '<name>-old 2' and so on,
    the one whose data was changed most recently."""
    candidates = [p for p in root.parent.glob(root.name + '-old*')
                  if p.is_dir() and (p / '.appbuilder').is_dir() and p.resolve() != Path(root).resolve()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / '.appbuilder').stat().st_mtime)


def carry_data(root=ROOT, data=DATA):
    previous = previous_folder(root)
    if previous is None:
        return None
    if data.exists() and any(data.iterdir()):
        if _task_count(data) > 0:
            return None                              # this folder already has its own builds
        if _task_count(previous / '.appbuilder') == 0:
            return None                              # nothing worth bringing over either
        aside = data.with_name(f'.appbuilder-before-update-{datetime.now():%Y%m%d%H%M%S}')
        data.rename(aside)
        say(f'本文件夹的数据目录里没有任何任务（多半是旧进程写进来的），已挪到 {aside.name}，改为复制上一个版本的数据')
    elif data.exists():
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
    """The project folder a service runs from. Its working directory comes first: it follows
    the folder when the old version was renamed to <name>-old, while the command line still
    shows the old name (which is now the NEW folder). The venv path is the fallback."""
    try:
        out = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ''
    for line in out.splitlines():
        if line.startswith('n/'):
            return Path(line[1:]).resolve()
    match = re.search(r'(/\S+?)/\.venv/bin/python', command)
    if match:
        return Path(match[1]).resolve()
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
    for step in (stop_other_folders, carry_data, repoint_launch_agent):
        try:
            step()
        except Exception as exc:                     # never block starting the page
            say(f'{step.__name__} 未完成：{type(exc).__name__}: {exc}')
