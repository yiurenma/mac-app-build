"""概览: what is running, what was delivered last, and what needs the person's attention.

Read-only summary built from data that already exists (tasks, their saved reports, the
schedules database and the Jira / email settings). Nothing secret is returned.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .telemetry import safe_text

FAILED = ('FAILED', 'NEEDS_ATTENTION', 'TIMEOUT', 'INTERRUPTED')
STEPS = [('Plugin 构建', ('PLUGIN_PREPARE', 'PLUGIN_CHECKOUT', 'PLUGIN_EDIT', 'PLUGIN_PUSH', 'PLUGIN_JENKINS')),
         ('GMX 构建', ('GMX_CHECKOUT', 'GMX_EDIT', 'GMX_LOCK', 'GMX_PUSH', 'GMX_JENKINS')),
         ('取 App 链接', ('TESTFAIRY_LOOKUP',))]


def _https(value):
    return value if isinstance(value, str) and value.startswith('https://') else ''


def _runtime(task_id):
    from .store import read_state
    try:
        return read_state(task_id)
    except (OSError, ValueError):
        return {'completed': [], 'runtime': {}}


def _progress(row, state):
    """Plugin / GMX / App link / Jira / email: each done, current or waiting."""
    done = set(state.get('completed') or [])
    out = []
    for label, stages in STEPS:
        if stages[-1] in done:
            out.append({'label': label, 'state': 'done'})
        elif row['stage'] in stages:
            out.append({'label': label, 'state': 'current'})
        else:
            out.append({'label': label, 'state': 'waiting'})
    rt = state.get('runtime', {})
    out.append({'label': '回填 Jira', 'state': 'done' if rt.get('jira_report') else 'waiting'})
    out.append({'label': '发邮件', 'state': 'done' if rt.get('jira_mail') else 'waiting'})
    return out


def _delivery(row, runtime):
    report = runtime.get('jira_report') or {}
    mail = runtime.get('jira_mail') or {}
    return {'task_id': row['id'], 'created': row['created'],
            'link': _https((runtime.get('testfairy_app') or {}).get('share_url')),
            'jira_status': report.get('status') or '',
            'tickets': [t.get('key') for t in report.get('tickets') or []][:20],
            'mail_status': mail.get('status') or '', 'mail_to': len(mail.get('to') or [])}


def _schedules(now):
    from . import schedules
    from .scheduler import heartbeat_status
    items, missed = [], []
    try:
        rows = schedules.list_schedules()
    except Exception:
        rows = []
    for row in rows:
        items.append({'name': row['name'], 'cron': row['cron'], 'enabled': bool(row['enabled']),
                      'next_run': row['next_run']})
        try:
            for run in schedules.list_runs(row['id'], limit=10):
                if run['missed'] and run['created'] >= (now - timedelta(days=1)).isoformat(timespec='seconds'):
                    missed.append({'name': row['name'], 'count': run['missed'], 'planned': run['planned']})
        except Exception:
            pass
    try:
        online = bool(heartbeat_status().get('online'))
    except Exception:
        online = False
    return {'items': items, 'online': online}, missed


def _automation():
    from .config import load_config
    from .jira_notify import settings
    try:
        conf = settings(load_config())
    except Exception:
        return {}
    return {'jira_enabled': bool(conf.get('enabled')), 'jira_dry_run': bool(conf.get('dry_run')),
            'mail_enabled': bool(conf.get('mail_enabled')), 'mail_failure_once': conf.get('mail_failure_once', True)}


def overview(now=None):
    from .store import task_list
    now = now or datetime.now()
    from .cli import alive
    tasks = []
    for t in task_list():     # a RUNNING row whose worker died is shown as interrupted, like the task list
        if t['status'] in ('RUNNING', 'QUEUED') and t.get('pid') and not alive(t['pid']):
            t = {**t, 'status': 'INTERRUPTED'}
        tasks.append(t)
    running = next((t for t in tasks if t['status'] in ('RUNNING', 'QUEUED')), None)
    last_success = next((t for t in tasks if t['status'] == 'SUCCESS'), None)
    attention = []
    # Failures newer than the last success still need a look; older ones were overtaken.
    for t in tasks:
        if last_success and t['created'] <= last_success['created']:
            break
        if t['status'] in FAILED:
            attention.append({'kind': 'task', 'task_id': t['id'], 'status': t['status'], 'stage': t['stage'],
                              'text': safe_text(t.get('note') or '')[:200]})
        if len(attention) >= 5:
            break
    out = {'running': None, 'last': None}
    if running:
        state = _runtime(running['id'])
        out['running'] = {'task_id': running['id'], 'created': running['created'], 'status': running['status'],
                          'steps': _progress(running, state)}
    if last_success:
        runtime = _runtime(last_success['id']).get('runtime', {})
        out['last'] = _delivery(last_success, runtime)
        for item in (runtime.get('jira_report') or {}).get('needs_manual') or []:
            attention.append({'kind': 'jira', 'key': item.get('key'), 'text': safe_text(item.get('reason') or '')[:200]})
    out['schedules'], missed = _schedules(now)
    attention += [{'kind': 'missed', **m} for m in missed]
    out['attention'] = attention
    out['automation'] = _automation()
    out['counts'] = {'total': len(tasks), 'running': sum(t['status'] in ('RUNNING', 'QUEUED') for t in tasks),
                     'success': sum(t['status'] == 'SUCCESS' for t in tasks),
                     'attention': sum(t['status'] in FAILED for t in tasks)}
    return out


def handle_get(path, query):
    if path == '/api/overview':
        return 200, overview()
    return None
