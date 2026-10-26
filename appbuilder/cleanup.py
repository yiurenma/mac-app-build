"""Reclaim disk used by finished tasks' git working copies.

Every task clones the Plugin and GMX repositories (full history + working tree)
into ``.appbuilder/runs/<task id>/{plugin,gmx}``. Once a task has SUCCEEDED those
copies are dead weight (a SUCCESS task can never be resumed), and unattended
schedules would otherwise add two more full clones on every run.

Only the ``plugin/`` and ``gmx/`` folders are ever removed. Logs, screenshots,
``state.json`` and ``result.json`` are always kept. Nothing here touches the
build flow; it is invoked by ``./app-build cleanup`` and by the scheduler.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from datetime import datetime, timedelta

from . import store

WORKDIRS = ('plugin', 'gmx')
MARKER = '.workdirs-removed'
ACTIVE = ('RUNNING', 'QUEUED')
DEFAULT_KEEP_DAYS = 7


def human(size):
    size = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def disk_usage(path):
    """Bytes actually allocated under ``path``; never follows symlinks."""
    total, stack = 0, [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    blocks = getattr(info, 'st_blocks', None)
                    total += blocks * 512 if blocks is not None else info.st_size
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            continue
    return total


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def plan(*, keep_days=DEFAULT_KEEP_DAYS, only_task_ids=None, now=None):
    """Work copies that are safe to remove right now (nothing is deleted here)."""
    now = now or datetime.now()
    items = []
    for row in store.task_list():
        task_id, status = row['id'], row['status']
        if only_task_ids is not None and task_id not in only_task_ids:
            continue
        if status in ACTIVE and _alive(row.get('pid')):
            continue                                   # a worker is using it
        if status == 'SUCCESS':
            reason = '已成功完成，不会再恢复'
        else:
            try:
                age = now - datetime.fromisoformat(row['created'])
            except (ValueError, TypeError):
                continue
            if age < timedelta(days=keep_days):
                continue                               # may still be resumed
            reason = f'{status}，已超过 {keep_days} 天'
        home = store.task_folder(task_id)
        paths = []
        for name in WORKDIRS:
            path = home / name
            if path.is_symlink() or not path.is_dir() or path.resolve().parent != home.resolve():
                continue
            paths.append(path)
        if paths:
            items.append({'task_id': task_id, 'status': status, 'reason': reason, 'paths': paths,
                          'bytes': sum(disk_usage(p) for p in paths)})
    return items


def _remove(path):
    def force(func, target, _error):
        os.chmod(target, stat.S_IRWXU)               # git packs are read-only
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, target, err: force(func, target, err))
    else:
        shutil.rmtree(path, onerror=force)


def apply(items):
    """Remove the planned work copies. Returns {'tasks', 'freed', 'errors'}."""
    freed, tasks, errors = 0, 0, []
    for item in items:
        removed = False
        for path in item['paths']:
            before = disk_usage(path)
            try:
                _remove(path)
            except OSError as exc:
                errors.append(f"{item['task_id']}/{path.name}: {exc}")
                continue
            freed += before
            removed = True
        if removed:
            tasks += 1
            try:
                (store.task_folder(item['task_id']) / MARKER).write_text(
                    datetime.now().isoformat(timespec='seconds') + '\n', encoding='utf-8')
            except OSError:
                pass
    return {'tasks': tasks, 'freed': freed, 'errors': errors}


def breakdown():
    """(run count, bytes in git work copies, bytes in everything else) under runs/."""
    runs = store.DATA / 'runs'
    count = work = other = 0
    if runs.is_dir():
        for home in runs.iterdir():
            if not home.is_dir() or home.is_symlink():
                continue
            count += 1
            for child in home.iterdir():
                if child.is_symlink():
                    continue
                size = disk_usage(child) if child.is_dir() else child.lstat().st_size
                if child.name in WORKDIRS:
                    work += size
                else:
                    other += size
    return count, work, other


def auto_cleanup(config):
    """Scheduler hook: reclaim only tasks that schedules created (unattended)."""
    from . import schedules
    build = (config or {}).get('build', {})
    if not build.get('auto_cleanup_scheduled_workdirs', True):
        return None
    ids = schedules.scheduled_task_ids()
    if not ids:
        return None
    return apply(plan(keep_days=int(build.get('workdir_retention_days', DEFAULT_KEEP_DAYS)),
                      only_task_ids=ids))
