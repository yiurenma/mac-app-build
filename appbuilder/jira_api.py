"""HTTP-facing helpers for the Jira panel; routed from web.py (Host/Origin/CSRF already checked).

Everything the page can do is here: read/save settings, store the token in the Keychain,
test the connection, look up one ticket (read-only) and rehearse on a finished build.
The token is never returned to the page.
"""
from __future__ import annotations
import json
import os
import re
from urllib.parse import urlsplit
from . import jira_notify as J
from . import jira_mail as M
from .config import load_config
from .telemetry import safe_text

KEY = re.compile(r'[A-Z][A-Z0-9]{1,9}-\d{1,7}')


def _token_saved():
    if os.environ.get('JIRA_TOKEN', '').strip():
        return 'env'
    try:
        from .auth import read_jira_token
        return 'keychain' if read_jira_token() else ''
    except Exception:
        return ''


def public_settings():
    conf = J.settings(load_config())
    return {'settings': {k: conf.get(k) for k in J.EDITABLE}, 'token_source': _token_saved()}


def _clean(body):
    out = {}
    for key, kind in J.EDITABLE.items():
        if key not in body:
            continue
        value = body[key]
        if kind is str:
            limit = 2000 if key in ('transition_fields', 'mail_users', 'comment_mentions') else 512
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'{key} 必须是文本（不超过 {limit} 字符）')
            value = value.strip()
        elif not J._type_ok(key, value):
            raise ValueError(f'{key} 类型不正确')
        out[key] = value
    if 'base_url' in out and out['base_url']:
        parts = urlsplit(out['base_url'])
        if parts.scheme != 'https' or not parts.netloc or parts.query or parts.fragment:
            raise ValueError('Jira 地址必须是 https 开头的网址，例如 https://wpb-jira.systems.uk.hsbc')
        out['base_url'] = out['base_url'].rstrip('/')
    if 'flavor' in out and out['flavor'] not in ('server', 'cloud'):
        raise ValueError('flavor 只能是 server 或 cloud')
    if 'key_pattern' in out:
        try:
            re.compile(out['key_pattern'])
        except re.error:
            raise ValueError('Jira 编号识别规则不是有效的正则表达式') from None
    if 'transition_fields' in out:
        J.parse_field_rules(out['transition_fields'])      # raises ValueError naming the bad line
    if 'mail_issue' in out:
        out['mail_issue'] = M.check_issue(out['mail_issue'])
    if 'comment_mentions' in out:
        M.parse_users(out['comment_mentions'])               # same rule: Jira user names only
    if 'mail_users' in out:
        M.parse_users(out['mail_users'])                     # raises ValueError naming the bad names
    if 'transition_name' in out and not out['transition_name']:
        raise ValueError('流转名称不能为空')
    if 'first_run_commits' in out and not 0 <= out['first_run_commits'] <= 200:
        raise ValueError('首次回溯提交数必须在 0–200 之间')
    if 'max_tickets_per_run' in out and not 1 <= out['max_tickets_per_run'] <= 200:
        raise ValueError('每轮最多处理 ticket 数必须在 1–200 之间')
    return out


def save_settings(body):
    clean = _clean(body)
    token = body.get('token')
    if token not in (None, ''):
        if not isinstance(token, str):
            raise ValueError('Token 必须是文本')
        from .auth import store_jira_token, CredentialError
        try:
            store_jira_token(token)
        except CredentialError as exc:
            raise ValueError(str(exc)) from None
    current = J.saved_settings()
    current.update(clean)
    J.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = J.SETTINGS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, J.SETTINGS_FILE)
    return public_settings()


def _client():
    conf = J.settings(load_config())
    if not conf.get('base_url'):
        raise ValueError('请先填写并保存 Jira 地址')
    return J.JiraClient(conf['base_url'], flavor=conf['flavor'], timeout=conf['timeout_seconds'],
                        ca_bundle=conf.get('ca_bundle')), conf


def test_connection(client=None):
    client = client or _client()[0]
    try:
        me = client.myself()
    except J.JiraNotFound:
        raise ValueError('Jira 地址不对（HTTP 404），请核对网址') from None
    except RuntimeError as exc:
        raise ValueError(safe_text(str(exc)) + '（401/403 通常是 Token 无效或没有权限）') from None
    return {'ok': True, 'user': me.get('displayName') or me.get('name') or '未知用户',
            'login': me.get('name') or me.get('accountId') or ''}


def lookup_ticket(key, client=None, conf=None):
    if not isinstance(key, str) or not KEY.fullmatch(key.strip()):
        raise ValueError('请输入形如 TRANSACT-210390 的 Jira 编号')
    key = key.strip()
    if client is None:
        client, conf = _client()
    conf = conf or J.settings(load_config())
    try:
        issue = client.issue(key)
    except J.JiraNotFound:
        return {'found': False, 'key': key,
                'message': 'Jira 里查不到这个编号（也可能是当前 Token 无权限查看）；正式运行时会直接忽略'}
    fields = issue.get('fields', {})
    reporter = fields.get('reporter') or {}
    wanted = conf['transition_name']
    issue_type = (fields.get('issuetype') or {}).get('name', '')
    options = client.transitions(key, expand=True)
    names = [t.get('name', '') for t in options]
    match = [t for t in options if t.get('name', '').strip().lower() == wanted.strip().lower()]
    required, missing = [], []
    if match:
        rules = J.parse_field_rules(conf.get('transition_fields'))
        payload, missing, bad, auto = J.plan_fields(match[0].get('fields'), issue_type, rules)
        for fid, meta in (match[0].get('fields') or {}).items():
            if meta.get('required') and not meta.get('hasDefaultValue'):
                allowed = [str(o.get('value') or o.get('name') or o.get('id')) for o in (meta.get('allowedValues') or [])]
                name = meta.get('name') or fid
                picked = next((a.split('=', 1)[1] for a in auto if a.split('=', 1)[0] == name), None)
                required.append({'name': name, 'allowed': allowed[:30], 'configured': fid in payload and picked is None,
                                 'auto': picked})
        missing = missing + bad
    return {'found': True, 'key': key, 'summary': fields.get('summary', ''),
            'status': (fields.get('status') or {}).get('name', ''),
            'issue_type': issue_type,
            'reporter': reporter.get('displayName') or reporter.get('name') or '无',
            'transitions': names,
            'can_transition': bool(match),
            'transition_name': wanted,
            'required_fields': required, 'problems': missing}


def preview(task_id, recent):
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('请选择一个已成功的历史构建')
    if type(recent) is not int or not 1 <= recent <= 100:
        raise ValueError('回看提交数必须是 1–100 的整数')
    return J.notify_jira(task_id, config=load_config(), preview=recent)


def handle_get(path, query):
    if path == '/api/jira/settings':
        return 200, public_settings()
    return None


def handle_post(path, body):
    try:
        if path == '/api/jira/settings':
            return 200, save_settings(body)
        if path == '/api/jira/test-connection':
            return 200, test_connection()
        if path == '/api/jira/ticket':
            return 200, lookup_ticket(body.get('key'))
        if path == '/api/jira/preview':
            return 200, preview(body.get('task_id'), body.get('recent', 10))
        if path == '/api/jira/mail-test':
            return 200, M.test_email(body)
    except ValueError as exc:
        return 400, {'error': safe_text(str(exc))}
    return None
