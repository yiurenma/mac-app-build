"""Cron-style recurring builds layered on top of the existing task engine.

Nothing here changes how a build runs: a due schedule simply calls the same
``web.create_build()`` that the "Start build" button uses, so every existing
validation, Keychain credential lookup, doctor check and concurrency limit
applies unchanged. Schedules store only a non-secret parameter template.

Semantics (decided with the user):
  * missed runs (computer asleep/off) collapse into ONE catch-up run: the latest;
  * when ``build.max_parallel`` is full a due run is QUEUED, never dropped, and
    starts as soon as a slot frees up (manual and scheduled builds share slots);
  * schedules are evaluated in local time.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import closing
from datetime import datetime

from . import cron, store
from .config import DATA, load_config, parallel_limit
from .telemetry import safe_text

MAX_SCHEDULES = 50
DEFAULT_MIN_INTERVAL_MINUTES = 30
SCHEMA = '''
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, params TEXT NOT NULL,
    next_run TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id TEXT NOT NULL,
    planned TEXT NOT NULL, status TEXT NOT NULL, task_id TEXT,
    note TEXT NOT NULL DEFAULT '', missed INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL, started TEXT, UNIQUE(schedule_id, planned));
'''

BASIC_KEYS = ('plugin_branch', 'gmx_branch', 'plugin_version', 'country', 'project_name')
OPTIONAL_TEXT = ('testfairy_login_url', 'testfairy_folder_path', 'gmx_issue_key', 'plugin_url', 'gmx_url', 'plugin_parent', 'gmx_parent', 'browser_channel',
                 'gmx_build_type', 'gmx_signing_repo_branch', 'gmx_custom_folder',
                 'upload_success_pattern',
                 'selector_entities', 'selector_sync_upload', 'selector_custom_folder',
                 'selector_build_type', 'selector_signing_repo_branch', 'selector_skip_test',
                 'selector_force', 'selector_keep_alive')
OPTIONAL_INT = ('discover_poll_seconds', 'poll_seconds', 'result_poll_seconds',
                'console_settle_seconds', 'discover_timeout_minutes', 'build_timeout_minutes',
                'callback_timeout_minutes', 'max_changed_lines')
OPTIONAL_BOOL = ('testfairy_enabled', 'jenkins_api', 'testfairy_save_credentials', 'visible', 'verbose', 'gmx_sync_upload', 'gmx_skip_test', 'gmx_force', 'gmx_keep_alive')
# Removed Outlook options: still accepted from schedules saved by older versions, then dropped.
LEGACY_IGNORED = ('outlook_auto_send', 'outlook_recipients', 'outlook_account')
SECRET_KEYS = ('testfairy_user', 'testfairy_password', 'jenkins_user', 'jenkins_password', 'github_token', 'jira_token', 'jenkins_api_token')


def connect():
    db = store.connect()
    try:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        additions = {
            'schedules': {'stop_after_success': 'INTEGER NOT NULL DEFAULT 0',
                          'generation': 'INTEGER NOT NULL DEFAULT 1',
                          'success_task_id': 'TEXT', 'stopped_at': 'TEXT', 'revision': 'INTEGER NOT NULL DEFAULT 0'},
            'schedule_runs': {'generation': 'INTEGER NOT NULL DEFAULT 1', 'claim_token': 'TEXT'},
        }
        for table, columns in additions.items():
            existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
            for name, definition in columns.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        db.commit()
    except BaseException:
        db.close()
        raise
    return db


def _iso(moment):
    return moment.isoformat(timespec='seconds')


def _parse_iso(text):
    return datetime.fromisoformat(text)


def validate_template(body, config=None):
    """Return a cleaned, validated build-parameter template (never any secret)."""
    from .web import form_defaults, validate_build_parameters
    if not isinstance(body, dict):
        raise ValueError('构建参数必须是对象')
    if any(key in body for key in SECRET_KEYS):
        raise ValueError('定时任务不保存账号、密码或 Token；请使用已保存在 Mac Keychain 的凭据')
    config = config if config is not None else load_config()
    body = {key: value for key, value in body.items() if key not in LEGACY_IGNORED}
    allowed = set(BASIC_KEYS + OPTIONAL_TEXT + OPTIONAL_INT + OPTIONAL_BOOL)
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(f'不支持的构建参数: {", ".join(unknown)}')
    defaults = form_defaults(config)
    template = {key: value for key, value in defaults.items() if key in allowed}
    template.update({'selector_' + key.lower(): value
                     for key, value in defaults['gmx_fields'].items()})
    template.update(body)
    # Freeze the same defaults and validate through the immediate-build path.
    params = validate_build_parameters(template, config)
    # APP links now come from the same Jenkins session as the build. Legacy
    # TestFairy settings remain compatible, but no separate login is required.
    template['country'] = template['country'].strip().upper()
    return template


def check_cron(expression, now=None, config=None):
    """Validate the expression and refuse accidentally aggressive schedules."""
    spec = cron.parse(expression)
    now = now or datetime.now()
    first = cron.next_after(spec, now)
    if first is None:
        raise cron.CronError('这个 cron 在未来 5 年内没有任何运行时间')
    config = config if config is not None else load_config()
    minimum = int(config.get('build', {}).get('schedule_min_interval_minutes', DEFAULT_MIN_INTERVAL_MINUTES))
    gap = cron.minimum_interval_minutes(expression, now)
    if gap is not None and gap < minimum:
        raise cron.CronError(f'运行间隔最短 {gap} 分钟，小于允许的最小 {minimum} 分钟；'
                             '每次运行都会创建新的分支和 Jenkins 构建')
    return spec, first


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 60:
        raise ValueError('名称不能为空且不超过 60 个字符')
    return value.strip()


def create_schedule(name, expression, template, *, enabled=True, stop_after_success=True, now=None, config=None):
    config = config if config is not None else load_config()
    now = now or datetime.now()
    name = _name(name)
    if not isinstance(stop_after_success, bool):
        raise ValueError("stop_after_success 必须是 true 或 false")
    _, first = check_cron(expression, now, config)
    expression = expression.strip()
    template = validate_template(template, config)
    schedule_id = 'sch-' + os.urandom(4).hex()
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT COUNT(*) FROM schedules').fetchone()[0] >= MAX_SCHEDULES:
            raise ValueError(f'最多保存 {MAX_SCHEDULES} 个定时任务')
        db.execute('INSERT INTO schedules(id,name,cron,enabled,params,next_run,created,updated,stop_after_success) '
                   'VALUES(?,?,?,?,?,?,?,?,?)',
                   (schedule_id, name, expression, int(bool(enabled)), json.dumps(template),
                    _iso(first) if enabled else None, _iso(now), _iso(now), int(stop_after_success)))
    return get_schedule(schedule_id)


def get_schedule(schedule_id):
    with closing(connect()) as db, db:
        row = db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
        if not row:
            raise ValueError('定时任务不存在: ' + schedule_id)
        return dict(row)


def list_schedules():
    with closing(connect()) as db, db:
        return [dict(r) for r in db.execute('SELECT * FROM schedules ORDER BY created')]


def update_schedule(schedule_id, *, name=None, expression=None, template=None, enabled=None,
                    stop_after_success=None, expected_revision=None, restart_cycle=False, now=None, config=None):
    config = config if config is not None else load_config()
    now = now or datetime.now()
    clean_template = validate_template(template, config) if template is not None else None
    if stop_after_success is not None and not isinstance(stop_after_success, bool):
        raise ValueError('stop_after_success 必须是 true 或 false')
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
        if current is None:
            raise ValueError('定时任务不存在: ' + schedule_id)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != current['revision']):
            raise ValueError('计划已变化（可能已成功停止）；请重新打开编辑后保存')
        if current['success_task_id'] and enabled is True and not restart_cycle:
            raise ValueError('本轮已成功停止；请使用列表的重新启用开启新一轮')
        new_name = _name(name) if name is not None else current['name']
        new_cron = expression if expression is not None else current['cron']
        _, first = check_cron(new_cron, now, config)
        new_enabled = bool(current['enabled']) if enabled is None else bool(enabled)
        new_stop = bool(current['stop_after_success']) if stop_after_success is None else stop_after_success
        # Explicit re-enable starts a fresh generation. Old workers cannot stop it.
        enabling_stop = new_stop and not current['stop_after_success']
        restart = new_enabled and (not current['enabled'] or enabling_stop)
        new_round = restart or enabling_stop
        generation = current['generation'] + int(new_round)
        next_run = (_iso(first) if restart or expression is not None else current['next_run']) if new_enabled else None
        db.execute('UPDATE schedules SET name=?,cron=?,params=?,enabled=?,next_run=?,updated=?, '
                   'stop_after_success=?,generation=?,success_task_id=?,stopped_at=?,revision=revision+1 WHERE id=?',
                   (new_name, new_cron.strip(), json.dumps(clean_template) if clean_template is not None else current['params'],
                    int(new_enabled), next_run, _iso(now), int(new_stop), generation,
                    None if new_round else current['success_task_id'], None if new_round else current['stopped_at'], schedule_id))
        if new_round or not new_enabled:
            db.execute("UPDATE schedule_runs SET status='CANCELLED',note='计划已停用或开启新一轮',claim_token=NULL "
                       "WHERE schedule_id=? AND status IN ('PENDING','CLAIMED')", (schedule_id,))
        reconcile_successes(db, now=now)
    return get_schedule(schedule_id)


def delete_schedule(schedule_id):
    get_schedule(schedule_id)
    with closing(connect()) as db, db:
        db.execute('DELETE FROM schedule_runs WHERE schedule_id=?', (schedule_id,))
        db.execute('DELETE FROM schedules WHERE id=?', (schedule_id,))


def list_runs(schedule_id, limit=20):
    with closing(connect()) as db, db:
        return [dict(r) for r in db.execute(
            'SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY planned DESC, id DESC LIMIT ?',
            (schedule_id, int(limit)))]


def last_run(schedule_id):
    runs = list_runs(schedule_id, 1)
    return runs[0] if runs else None


def run_now(schedule_id, now=None):
    """Manual runs of a success-stopped cycle require an explicit new cycle."""
    now = now or datetime.now()
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        reconcile_successes(db, now=now)
        row = db.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
        if row is None:
            raise ValueError('定时任务不存在: ' + schedule_id)
        if row['success_task_id']:
            raise ValueError('此轮已成功停止；请先重新启用，开启新一轮')
        cur = db.execute("INSERT OR IGNORE INTO schedule_runs(schedule_id,planned,status,note,created,generation) "
                         "VALUES(?,?, 'PENDING', '手动触发', ?,?)", (schedule_id, _iso(now), _iso(now), row['generation']))
        if cur.rowcount == 0:
            raise ValueError('刚刚已经触发过一次，请稍候再试')
    return {'queued': True, 'run_id': cur.lastrowid, 'status': 'PENDING'}


def enqueue_due(now):
    """Turn every due schedule into at most ONE pending run and advance it."""
    queued = 0
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        reconcile_successes(db, now=now)
        due = db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?',
                         (_iso(now),)).fetchall()
        for row in due:
            spec = cron.parse(row['cron'])
            planned, missed = cron.latest_due(spec, _parse_iso(row['next_run']), now)
            note = f'错过 {missed} 次，仅补跑最近一次' if missed else ''
            cur = db.execute("INSERT OR IGNORE INTO schedule_runs(schedule_id,planned,status,note,missed,created,generation) "
                             "VALUES(?,?, 'PENDING', ?, ?, ?, ?)",
                             (row['id'], _iso(planned), note, missed, _iso(now), row['generation']))
            queued += cur.rowcount
            following = cron.next_after(spec, now)
            db.execute('UPDATE schedules SET next_run=? WHERE id=?',
                       (_iso(following) if following else None, row['id']))
    return queued


def _is_capacity_error(exc):
    return '并发' in str(exc)


def _default_start(body, claim):
    from .web import create_build
    return create_build(body, schedule_claim=claim)


class RunUnavailable(ValueError):
    """A cancelled, already-owned or obsolete run must never launch a new task."""


def reconcile_successes(db, *, now=None):
    """Called within the caller's write transaction, including task SUCCESS commit."""
    # Ordinary manual tasks can finish before any schedules have been created.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule_runs'").fetchone():
        return
    if 'generation' not in {r['name'] for r in db.execute('PRAGMA table_info(schedule_runs)')}:
        return  # scheduler startup migrates and reconciles older databases
    rows = db.execute("SELECT s.id,t.id AS task_id FROM schedules s "
                      "JOIN schedule_runs r ON r.schedule_id=s.id AND r.generation=s.generation "
                      "JOIN tasks t ON t.id=r.task_id WHERE s.stop_after_success=1 "
                      "AND s.success_task_id IS NULL AND t.status='SUCCESS' AND t.stage='FINISHED' "
                      "ORDER BY r.id").fetchall()
    for row in rows:
        changed = db.execute('UPDATE schedules SET enabled=0,next_run=NULL,success_task_id=?,stopped_at=?,updated=?,revision=revision+1 '
                             'WHERE id=? AND success_task_id IS NULL',
                             (row['task_id'], _iso(now or datetime.now()), _iso(now or datetime.now()), row['id'])).rowcount
        if changed:
            db.execute("UPDATE schedule_runs SET status='CANCELLED',note='本轮已成功，取消未启动运行',claim_token=NULL "
                       "WHERE schedule_id=? AND status IN ('PENDING','CLAIMED')", (row['id'],))


def check_resume_slot(db, task_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedules'").fetchone():
        return
    if 'stop_after_success' not in {r['name'] for r in db.execute('PRAGMA table_info(schedules)')}:
        return
    owner = db.execute('SELECT r.schedule_id FROM schedule_runs r JOIN schedules s ON s.id=r.schedule_id '
                       'WHERE r.task_id=? AND s.stop_after_success=1', (task_id,)).fetchone()
    if owner and db.execute("SELECT 1 FROM schedule_runs r LEFT JOIN tasks t ON t.id=r.task_id "
                            "WHERE r.schedule_id=? AND (r.task_id IS NULL OR r.task_id<>?) "
                            "AND (r.status='CLAIMED' OR t.status IN ('QUEUED','RUNNING')) LIMIT 1",
                            (owner['schedule_id'], task_id)).fetchone():
        raise ValueError('本计划已有未完成任务，不能同时恢复另一任务')


def recover_startup():
    """Only the exclusive scheduler owner may release pre-task crash claims."""
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        reconcile_successes(db)
        db.execute("UPDATE schedule_runs SET status='PENDING',claim_token=NULL "
                   "WHERE status='CLAIMED' AND task_id IS NULL")
        # STARTED tasks are never automatically spawned again: their worker may
        # already exist, or have dispatched Jenkins before a local crash.


def _claim_pending(run_id, now):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        reconcile_successes(db, now=now)
        row = db.execute('SELECT r.*,s.params,s.enabled,s.stop_after_success,s.success_task_id, '
                         's.generation AS current_generation FROM schedule_runs r '
                         'JOIN schedules s ON s.id=r.schedule_id WHERE r.id=?', (run_id,)).fetchone()
        if row is None or row['status'] != 'PENDING':
            return None
        if row['generation'] != row['current_generation'] or row['success_task_id']:
            db.execute("UPDATE schedule_runs SET status='CANCELLED',note='旧轮次或已成功停止' WHERE id=?", (run_id,))
            return None
        if not row['enabled'] and row['note'] != '手动触发':
            db.execute("UPDATE schedule_runs SET status='CANCELLED',note='计划已停用' WHERE id=?", (run_id,))
            return None
        if row['stop_after_success'] and db.execute(
                "SELECT 1 FROM schedule_runs r LEFT JOIN tasks t ON t.id=r.task_id "
                "WHERE r.schedule_id=? AND r.id<>? AND (r.status='CLAIMED' OR t.status IN ('QUEUED','RUNNING')) LIMIT 1",
                (row['schedule_id'], run_id)).fetchone():
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE schedule_runs SET status='CLAIMED',claim_token=? WHERE id=?", (token, run_id))
        return (run_id, token), json.loads(row['params'])


def attach_task(db, claim, task_id):
    """Link under the SAME transaction that inserts the task, before spawn."""
    run_id, token = claim
    reconcile_successes(db)
    row = db.execute('SELECT r.*,s.generation AS current_generation,s.success_task_id,s.stop_after_success FROM schedule_runs r '
                     'JOIN schedules s ON s.id=r.schedule_id WHERE r.id=?', (run_id,)).fetchone()
    if (row is None or row['status'] != 'CLAIMED' or row['claim_token'] != token
            or row['generation'] != row['current_generation'] or row['success_task_id']):
        raise RunUnavailable('运行已取消、已认领或所属轮次已停止')
    if row['stop_after_success'] and db.execute(
            "SELECT 1 FROM schedule_runs r JOIN tasks t ON t.id=r.task_id "
            "WHERE r.schedule_id=? AND r.id<>? AND t.status IN ('QUEUED','RUNNING') LIMIT 1",
            (row['schedule_id'], run_id)).fetchone():
        raise RunUnavailable('本计划已有未完成任务，本次不重复启动')
    db.execute("UPDATE schedule_runs SET task_id=?,status='STARTED',started=?,claim_token=NULL WHERE id=?",
               (task_id, _iso(datetime.now()), run_id))


def _release_claim(claim, status, note='', task_id=None, now=None):
    # Never overwrite a real task's already-committed association after spawn.
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE schedule_runs SET status=?,claim_token=NULL,task_id=?, '
                   "note=CASE WHEN ?<>'' THEN ? ELSE note END,started=? "
                   "WHERE id=? AND status='CLAIMED' AND claim_token=?",
                   (status, task_id, note, note, _iso(now or datetime.now()), *claim))


def dispatch_pending(now=None, start=None, config=None):
    now = now or datetime.now()
    config = config if config is not None else load_config()
    limit = parallel_limit(config)
    with closing(connect()) as db:
        pending = [r['id'] for r in db.execute("SELECT id FROM schedule_runs WHERE status='PENDING' ORDER BY planned,id")]
    started = 0
    for run_id in pending:
        if store.active_count() >= limit:
            print(f'[SCHEDULER] run_id={run_id} PENDING；并发已满，尚未创建新构建/Worker', flush=True)
            break
        claimed = _claim_pending(run_id, now)
        if claimed is None:
            continue
        claim, template = claimed
        try:
            body = validate_template(template, config)
            created = _default_start(body, claim) if start is None else start(body)
        except Exception as exc:
            busy = _is_capacity_error(exc)
            _release_claim(claim, 'PENDING' if busy else 'FAILED',
                           '' if busy else safe_text(f'{type(exc).__name__}: {exc}')[:500], now=now)
            if not busy:
                with closing(connect()) as db, db:
                    task = db.execute('SELECT t.status FROM tasks t JOIN schedule_runs r ON r.task_id=t.id WHERE r.id=?', (run_id,)).fetchone()
                    note = ('Worker 未启动；下次计划可继续：' if task and task['status'] == 'FAILED'
                            else '任务已创建，启动结果需人工核对；不自动重发：')
                    db.execute("UPDATE schedule_runs SET note=? WHERE id=? AND status='STARTED' AND task_id IS NOT NULL",
                               (note + safe_text(str(exc))[:300], run_id))
            if busy:
                break
            continue
        # Injectable offline starters do not create real tasks. Production has
        # already attached its task atomically inside store.create_task().
        if start is not None:
            _release_claim(claim, 'STARTED', task_id=created['id'], now=now)
        print(f'[SCHEDULER] run_id={run_id} → task_id={created["id"]}；日志 {store.task_folder(created["id"])/"run.log"}', flush=True)
        started += 1
    return started


def tick(now=None, *, start=None, config=None):
    now = now or datetime.now()
    queued = enqueue_due(now)
    started = dispatch_pending(now, start=start, config=config)
    return {'queued': queued, 'started': started}
