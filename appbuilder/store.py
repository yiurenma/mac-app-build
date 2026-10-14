from __future__ import annotations
from contextlib import closing
import json
import os
import sqlite3
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from .config import DATA, parallel_limit
from .lifecycle import admitted
from .versions import branch_names, job_urls, plugin_parent_for_branch


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'tasks.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA busy_timeout=30000')
        # Concurrent first connections may get SQLITE_BUSY while changing the
        # journal mode, even when sqlite's busy timeout is configured.
        deadline = time.monotonic() + 30
        while True:
            try:
                db.execute('PRAGMA journal_mode=WAL')
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        db.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, created TEXT NOT NULL, stamp TEXT NOT NULL,
            status TEXT NOT NULL, pid INTEGER, params TEXT NOT NULL,
            stage TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')''')
        db.commit()
    except BaseException:
        db.close()
        raise
    return db


@admitted
def create_task(config, params, *, schedule_claim=None):
    """Reserve a unique timestamp and initialize task state in one DB transaction.

    On failed filesystem initialization, roll back the reservation and clean up.
    """
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        running = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
        if running >= parallel_limit(config):
            raise RuntimeError('并发任务已达上限；请先等待当前任务完成')
        existing = {r['stamp'] for r in db.execute('SELECT stamp FROM tasks')}
        stamp = datetime.now().strftime('%Y%m%d%H%M')
        while stamp in existing:
            stamp = (datetime.strptime(stamp, '%Y%m%d%H%M') + timedelta(minutes=1)).strftime('%Y%m%d%H%M')
        task_id = f"{stamp}-{os.urandom(3).hex()}"
        generated = branch_names(params['plugin_version_input'], stamp, params['gmx_source'], params.get('gmx_issue_key') or config.get('build', {}).get('gmx_issue_key', ''))
        if generated['plugin_branch'] == params.get('plugin_source'):
            raise ValueError('Plugin 来源分支不能与构建目标相同')
        # Resolve branch Jobs using this task's explicit Jenkins defaults, not
        # mutable process-global config. Other concurrent tasks remain isolated.
        job_config = {**config, 'jenkins': {**config['jenkins'], **{
            k: v for k, v in params.get('jenkins_settings', {}).items()
            if k in ('plugin_parent', 'gmx_parent')
        }}}
        old_parent = job_config['jenkins']['plugin_parent']
        parent = plugin_parent_for_branch(old_parent, generated['plugin_branch'])
        if parent != old_parent:
            # Saved forms/schedules may still contain the reverted feature default.
            # Snapshot the new parent only on this newly-created task, not its template.
            job_config['jenkins']['plugin_parent'] = parent
            params = {**params, 'jenkins_settings': {**params.get('jenkins_settings', {}), 'plugin_parent': parent}}
        urls = job_urls(job_config, generated['plugin_branch'], generated['gmx_branch'])
        params = {**params, **generated, **urls, 'stamp': stamp, 'id': task_id, 'plugin_flow_version': 2}
        workdir = task_folder(task_id)
        # mkdir before INSERT: a missing working directory must never become
        # a committed QUEUED database row.
        workdir.mkdir(parents=True, exist_ok=False)
        try:
            save_state(task_id, {'completed': [], 'runtime': {'dispatch_policy':'receipt-v1'}})
            db.execute('INSERT INTO tasks(id,created,stamp,status,params,stage) VALUES(?,?,?,?,?,?)',
                       (task_id, datetime.now().isoformat(), stamp, 'QUEUED', json.dumps(params), 'INIT'))
            if schedule_claim is not None:
                from .schedules import attach_task
                attach_task(db, schedule_claim, task_id)
        except BaseException:
            shutil.rmtree(workdir)
            raise
    return params


def task(task_id):
    with closing(connect()) as db, db:
        r = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not r:
            raise ValueError('任务不存在: ' + task_id)
        return dict(r)


def task_list():
    with closing(connect()) as db, db:
        return [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY created DESC')]


@admitted
def reserve_resume(config, task_id):
    """Atomically reserve a shared slot before launching a resumed worker."""
    from .cli import alive
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise ValueError('任务不存在: ' + task_id)
        if row['status'] == 'SUCCESS':
            raise ValueError('任务已完成，无需恢复')
        if row['status'] in ('QUEUED', 'RUNNING') and alive(row['pid']):
            raise ValueError('原 Worker 仍在运行，禁止重复启动')
        count = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('QUEUED','RUNNING') AND id<>?",
                           (task_id,)).fetchone()[0]
        if count >= parallel_limit(config):
            raise ValueError('并发任务已达到上限')
        from .schedules import check_resume_slot
        check_resume_slot(db, task_id)
        # The launching process owns the reservation until the worker takes it.
        db.execute("UPDATE tasks SET status='QUEUED', pid=?, note='等待恢复 Worker' WHERE id=?",
                   (os.getpid(), task_id))
        return dict(row)


def update_task(task_id, **values):
    allowed = {'pid', 'status', 'stage', 'note'}
    if not values.keys() <= allowed:
        raise ValueError('unknown database update')
    with closing(connect()) as db, db:
        db.execute('UPDATE tasks SET ' + ', '.join(k+'=?' for k in values) + ' WHERE id=?',
                   (*values.values(), task_id))
        if values.get('status') == 'SUCCESS':
            from .schedules import reconcile_successes
            reconcile_successes(db)


def update_task_params(task_id, *, visible=None, verbose=None):
    """Persist per-task observation options before resuming a stopped worker."""
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        r = db.execute('SELECT params FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not r:
            raise ValueError('任务不存在: ' + task_id)
        params = json.loads(r['params'])
        if visible is not None:
            params['visible'] = bool(visible)
        if verbose is not None:
            params['verbose'] = bool(verbose)
        db.execute('UPDATE tasks SET params=? WHERE id=?', (json.dumps(params), task_id))


def task_folder(task_id):
    return DATA / 'runs' / task_id


def read_state(task_id):
    return json.loads((task_folder(task_id) / 'state.json').read_text(encoding='utf-8'))


def save_state(task_id, state):
    path = task_folder(task_id) / 'state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def patch_runtime(task_id, **values):
    s = read_state(task_id)
    s['runtime'].update(values)
    save_state(task_id, s)


def complete(task_id, step):
    s = read_state(task_id)
    if step not in s['completed']:
        s['completed'].append(step)
    save_state(task_id, s)


def active_count():
    return sum(r['status'] in ('QUEUED', 'RUNNING') for r in task_list())


def replace_gmx_target(task_id, old, new, job):
    """Persist the renamed target and Jenkins URL together under the worker lease."""
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT params FROM tasks WHERE id=?', (task_id,)).fetchone()
        params = json.loads(row['params'])
        if params['gmx_branch'] != old:
            raise RuntimeError('GMX 目标分支已改变，停止恢复')
        params.update(gmx_branch=new, gmx_job=job)
        db.execute('UPDATE tasks SET params=? WHERE id=?', (json.dumps(params), task_id))
    return params
