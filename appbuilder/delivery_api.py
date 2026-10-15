"""交付记录 page: which Jira tickets got which App link, and which emails went out.

Tickets come from each task's saved Jira report (newest task first); emails come from the
local mail log written by jira_mail. Read-only; nothing secret is stored in either.
"""
from __future__ import annotations

from .telemetry import safe_text

LIMIT = 300


def _https(value):
    return value if isinstance(value, str) and value.startswith('https://') else ''


def tickets(query='', limit=LIMIT):
    from .store import task_list, read_state
    q = (query or '').strip().lower()
    rows = []
    for row in task_list():
        try:
            runtime = read_state(row['id']).get('runtime', {})
        except (OSError, ValueError):
            continue
        report = runtime.get('jira_report') or {}
        if report.get('status') not in ('DONE', 'PARTIAL', 'DRY_RUN'):
            continue
        link = _https(report.get('link')) or _https((runtime.get('testfairy_app') or {}).get('share_url'))
        base = {'at': report.get('at') or row['created'], 'task_id': row['id'], 'link': link,
                'build': report.get('build') or runtime.get('gmx_build_number'),
                'dry_run': report.get('status') == 'DRY_RUN'}
        manual = {m.get('key'): m.get('reason') for m in report.get('needs_manual') or []}
        items = [{**base, 'key': t.get('key', ''), 'summary': t.get('summary', ''),
                  'steps': [safe_text(str(s))[:200] for s in t.get('steps') or []],
                  'result': 'DRY_RUN' if base['dry_run'] else ('MANUAL' if t.get('key') in manual else 'DONE'),
                  'note': safe_text(manual.get(t.get('key')) or '')[:200]}
                 for t in report.get('tickets') or []]
        items += [{**base, 'key': e.get('key', ''), 'summary': '', 'steps': [], 'result': 'ERROR',
                   'note': safe_text(e.get('error') or '')[:200]} for e in report.get('errors') or []]
        for item in items:
            if q and q not in f"{item['key']} {item['summary']} {item['task_id']}".lower():
                continue
            rows.append(item)
            if len(rows) >= limit:
                return rows
    return rows


def mails(query='', limit=LIMIT):
    from .jira_mail import read_mails
    return [{k: row.get(k) for k in ('at', 'kind', 'task_id', 'subject', 'to', 'status', 'reason', 'body', 'issue')}
            for row in read_mails(query, limit)]


def jira_base():
    from .config import load_config
    from .jira_notify import settings
    try:
        return settings(load_config()).get('base_url') or ''
    except Exception:
        return ''


def handle_get(path, query):
    q = (query.get('q') or [''])[0][:100]
    if path == '/api/delivery/tickets':
        return 200, {'items': tickets(q), 'jira_base': jira_base()}
    if path == '/api/delivery/mails':
        return 200, {'items': mails(q)}
    return None
