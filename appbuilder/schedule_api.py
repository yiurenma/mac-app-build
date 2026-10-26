"""HTTP-facing helpers for schedules; routed from web.py (CSRF/Host checks already done)."""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import cron, schedules
from .config import load_config
from .scheduler import heartbeat_status

SCHEDULE_ID = re.compile(r'^sch-[0-9a-f]{8}$')


def _task_status(task_id):
    if not task_id:
        return None
    try:
        from .web import public_task
        from .store import task
        return public_task(task(task_id))['status']
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 'MISSING'


def _public_run(run):
    return {'id': run['id'], 'planned': run['planned'], 'status': run['status'],
            'task_id': run['task_id'], 'task_status': _task_status(run['task_id']),
            'note': run['note'], 'missed': run['missed'], 'started': run['started']}


def public_schedule(row):
    template = json.loads(row['params'])
    run = schedules.last_run(row['id'])
    return {'id': row['id'], 'name': row['name'], 'cron': row['cron'], 'enabled': bool(row['enabled']),
            'next_run': row['next_run'], 'created': row['created'],
            'template': template,    # non-secret form values; the page re-fills the New build form from it
            'last_run': _public_run(run) if run else None}


def _schedule_id(value):
    if not isinstance(value, str) or not SCHEDULE_ID.fullmatch(value):
        raise ValueError('定时任务 ID 格式不合法')
    return value


def _preview(expression):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError('请输入 cron 表达式')
    schedules.check_cron(expression, datetime.now(), load_config())  # raises on bad/aggressive cron
    return [t.isoformat(timespec='minutes') for t in cron.next_runs(expression, datetime.now(), 5)]


def handle_get(path, query):
    """Return (status, payload) or None when the path is not a schedule route."""
    if path == '/api/schedules':
        config = load_config()
        return 200, {'schedules': [public_schedule(r) for r in schedules.list_schedules()],
                     'scheduler': heartbeat_status(),
                     'max_parallel': int(config.get('build', {}).get('max_parallel', 2)),
                     'min_interval_minutes': int(config.get('build', {}).get(
                         'schedule_min_interval_minutes', schedules.DEFAULT_MIN_INTERVAL_MINUTES))}
    if path == '/api/schedules/preview':
        try:
            return 200, {'next': _preview((query.get('cron') or [''])[0])}
        except cron.CronError as exc:
            raise ValueError(str(exc)) from None
    m = re.fullmatch(r'/api/schedules/([^/]+)/runs', path)
    if m:
        schedule_id = _schedule_id(m[1])
        schedules.get_schedule(schedule_id)
        return 200, {'runs': [_public_run(r) for r in schedules.list_runs(schedule_id, 20)]}
    return None


def _template_from(body):
    template = body.get('template')
    if not isinstance(template, dict):
        raise ValueError('缺少构建参数 template')
    return template


def handle_post(path, body):
    if path == '/api/schedules':
        row = schedules.create_schedule(body.get('name'), body.get('cron') or '', _template_from(body),
                                        enabled=body.get('enabled', True) is True)
        return 201, {'schedule': public_schedule(row)}
    m = re.fullmatch(r'/api/schedules/([^/]+)/(update|toggle|delete|run-now)', path)
    if not m:
        return None
    schedule_id, action = _schedule_id(m[1]), m[2]
    if action == 'update':
        row = schedules.update_schedule(
            schedule_id, name=body.get('name'), expression=body.get('cron'),
            template=_template_from(body) if 'template' in body else None,
            enabled=body['enabled'] if isinstance(body.get('enabled'), bool) else None)
        return 200, {'schedule': public_schedule(row)}
    if action == 'toggle':
        if not isinstance(body.get('enabled'), bool):
            raise ValueError('enabled 必须是 true 或 false')
        return 200, {'schedule': public_schedule(schedules.update_schedule(schedule_id, enabled=body['enabled']))}
    if action == 'delete':
        schedules.delete_schedule(schedule_id)
        return 200, {'deleted': schedule_id}
    return 202, schedules.run_now(schedule_id)
