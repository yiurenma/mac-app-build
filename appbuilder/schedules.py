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
from contextlib import closing
from datetime import datetime

from . import cron, store
from .config import load_config
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
OPTIONAL_TEXT = ('plugin_url', 'gmx_url', 'plugin_parent', 'gmx_parent', 'browser_channel',
                 'gmx_build_type', 'gmx_signing_repo_branch', 'gmx_custom_folder',
                 'upload_success_pattern',
                 'selector_entities', 'selector_sync_upload', 'selector_custom_folder',
                 'selector_build_type', 'selector_signing_repo_branch', 'selector_skip_test',
                 'selector_force', 'selector_keep_alive')
OPTIONAL_INT = ('discover_poll_seconds', 'poll_seconds', 'result_poll_seconds',
                'console_settle_seconds', 'discover_timeout_minutes', 'build_timeout_minutes',
                'callback_timeout_minutes', 'max_changed_lines')
OPTIONAL_BOOL = ('visible', 'verbose', 'gmx_sync_upload', 'gmx_skip_test', 'gmx_force', 'gmx_keep_alive')
SECRET_KEYS = ('jenkins_user', 'jenkins_password', 'github_token')


def connect():
    db = store.connect()
    try:
        db.executescript(SCHEMA)
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
    from .web import _field, build_preferences
    from .versions import validate_args
    if not isinstance(body, dict):
        raise ValueError('构建参数必须是对象')
    if any(key in body for key in SECRET_KEYS):
        raise ValueError('定时任务不保存账号、密码或 Token；请使用已保存在 Mac Keychain 的凭据')
    config = config or load_config()
    allowed = set(BASIC_KEYS) | set(OPTIONAL_TEXT) | set(OPTIONAL_INT) | set(OPTIONAL_BOOL)
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(f'不支持的构建参数: {", ".join(unknown)}')
    template = {}
    for key in BASIC_KEYS:
        template[key] = _field(body, key, 200 if 'branch' in key else 100)
    template['country'] = template['country'].upper()
    for key in OPTIONAL_TEXT:
        value = body.get(key)
        if value in (None, ''):
            continue
        if not isinstance(value, str):
            raise ValueError(f'{key} 必须是文本')
        template[key] = value.strip()
    for key in OPTIONAL_INT:
        value = body.get(key)
        if value in (None, ''):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f'{key} 必须是整数')
        template[key] = value
    for key in OPTIONAL_BOOL:
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f'{key} 必须是开关值')
            template[key] = body[key]
    # Same validators the New build form uses, so a saved template cannot fail
    # parameter validation later at 3 a.m.
    build_preferences(template, config, template['country'], template['project_name'])
    validate_args(template['plugin_version'], template['plugin_branch'], template['gmx_branch'],
                  template['country'], template['project_name'])
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


def create_schedule(name, expression, template, *, enabled=True, now=None, config=None):
    config = config if config is not None else load_config()
    now = now or datetime.now()
    name = _name(name)
    expression = expression.strip()
    _, first = check_cron(expression, now, config)
    template = validate_template(template, config)
    schedule_id = 'sch-' + os.urandom(4).hex()
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT COUNT(*) FROM schedules').fetchone()[0] >= MAX_SCHEDULES:
            raise ValueError(f'最多保存 {MAX_SCHEDULES} 个定时任务')
        db.execute('INSERT INTO schedules(id,name,cron,enabled,params,next_run,created,updated) '
                   'VALUES(?,?,?,?,?,?,?,?)',
                   (schedule_id, name, expression, int(bool(enabled)), json.dumps(template),
                    _iso(first) if enabled else None, _iso(now), _iso(now)))
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
                    now=None, config=None):
    config = config if config is not None else load_config()
    now = now or datetime.now()
    current = get_schedule(schedule_id)
    new_name = _name(name) if name is not None else current['name']
    new_cron = expression.strip() if expression is not None else current['cron']
    new_template = validate_template(template, config) if template is not None else json.loads(current['params'])
    new_enabled = bool(current['enabled']) if enabled is None else bool(enabled)
    _, first = check_cron(new_cron, now, config)
    # Any edit or re-enable restarts the clock from "now": no catch-up for the
    # period during which the schedule was off or being edited.
    reschedule = new_enabled and (expression is not None or not current['enabled'])
    next_run = (_iso(first) if reschedule else current['next_run']) if new_enabled else None
    with closing(connect()) as db, db:
        db.execute('UPDATE schedules SET name=?, cron=?, params=?, enabled=?, next_run=?, updated=? WHERE id=?',
                   (new_name, new_cron, json.dumps(new_template), int(new_enabled), next_run,
                    _iso(now), schedule_id))
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
    """Queue one extra run immediately (still subject to the concurrency limit)."""
    now = now or datetime.now()
    get_schedule(schedule_id)
    with closing(connect()) as db, db:
        cur = db.execute("INSERT OR IGNORE INTO schedule_runs(schedule_id,planned,status,note,created) "
                         "VALUES(?,?, 'PENDING', '手动触发', ?)", (schedule_id, _iso(now), _iso(now)))
        if cur.rowcount == 0:
            raise ValueError('刚刚已经触发过一次，请稍候再试')
    return {'queued': True}


def enqueue_due(now):
    """Turn every due schedule into at most ONE pending run and advance it."""
    queued = 0
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        due = db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?',
                         (_iso(now),)).fetchall()
        for row in due:
            spec = cron.parse(row['cron'])
            planned, missed = cron.latest_due(spec, _parse_iso(row['next_run']), now)
            note = f'错过 {missed} 次，仅补跑最近一次' if missed else ''
            cur = db.execute("INSERT OR IGNORE INTO schedule_runs(schedule_id,planned,status,note,missed,created) "
                             "VALUES(?,?, 'PENDING', ?, ?, ?)",
                             (row['id'], _iso(planned), note, missed, _iso(now)))
            queued += cur.rowcount
            following = cron.next_after(spec, now)
            db.execute('UPDATE schedules SET next_run=? WHERE id=?',
                       (_iso(following) if following else None, row['id']))
    return queued


def _is_capacity_error(exc):
    return '并发' in str(exc)


def _default_start(body):
    from .web import create_build
    return create_build(body)


def _finish(run_id, status, *, task_id=None, note='', now=None):
    now = now or datetime.now()
    with closing(connect()) as db, db:
        db.execute('UPDATE schedule_runs SET status=?, task_id=?, note=CASE WHEN ?<>\'\' THEN ? ELSE note END, '
                   'started=? WHERE id=?', (status, task_id, note, note, _iso(now), run_id))


def dispatch_pending(now=None, start=None, config=None):
    """Start queued runs oldest-first until the shared concurrency limit is hit."""
    start = start or _default_start
    now = now or datetime.now()
    config = config if config is not None else load_config()
    limit = int(config.get('build', {}).get('max_parallel', 2))
    with closing(connect()) as db, db:
        pending = [dict(r) for r in db.execute(
            "SELECT * FROM schedule_runs WHERE status='PENDING' ORDER BY planned, id")]
    started = 0
    for run in pending:
        if store.active_count() >= limit:
            break
        try:
            schedule = get_schedule(run['schedule_id'])
        except ValueError:
            _finish(run['id'], 'FAILED', note='定时任务已被删除', now=now)
            continue
        template = json.loads(schedule['params'])
        # Unattended: no browser window unless the template asks for one.
        body = {**template, 'visible': template.get('visible', False),
                'verbose': template.get('verbose', True)}
        try:
            created = start(body)
        except Exception as exc:  # noqa: BLE001 - recorded, never crashes the daemon
            if _is_capacity_error(exc):
                break
            _finish(run['id'], 'FAILED', note=safe_text(f'{type(exc).__name__}: {exc}')[:500], now=now)
            continue
        _finish(run['id'], 'STARTED', task_id=created['id'], now=now)
        started += 1
    return started


def tick(now=None, *, start=None, config=None):
    now = now or datetime.now()
    queued = enqueue_due(now)
    started = dispatch_pending(now, start=start, config=config)
    return {'queued': queued, 'started': started}
