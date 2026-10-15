"""Email the team through Jira after a successful build.

Jira Server / Data Center has a REST call that sends an email about an issue to chosen
users: POST /rest/api/2/issue/{key}/notify. The Jira server sends the mail, so no
Outlook, mail account or password is involved; the saved Jira token is enough.

The call must name an issue, so the team keeps one fixed "announcement" ticket and every
email goes out through it. One email per successful build (with an App link); failed builds
send nothing. A failure here never changes the build result, and nothing is retried
automatically.
"""
from __future__ import annotations
import json
import re
from urllib.parse import quote

from .config import load_config
from .store import task, read_state, patch_runtime
from .telemetry import action, safe_text

USER = re.compile(r'[A-Za-z0-9._@+-]{1,128}')
KEY = re.compile(r'[A-Z][A-Z0-9]{1,9}-\d{1,7}')
MAX_USERS = 50


def parse_users(text):
    """'a, b; c' or one per line -> ['a', 'b', 'c'] (order kept, duplicates dropped)."""
    if not isinstance(text, str):
        raise ValueError('收件人必须是文本')
    names = [n for n in re.split(r'[\s,;，；]+', text) if n]
    bad = [n for n in names if not USER.fullmatch(n)]
    if bad:
        raise ValueError('收件人只填 Jira 用户名（不是显示名），格式不对：' + '、'.join(bad[:5]))
    names = list(dict.fromkeys(names))
    if len(names) > MAX_USERS:
        raise ValueError(f'收件人最多 {MAX_USERS} 个')
    return names


def check_issue(key):
    if key is not None and not isinstance(key, str):
        raise ValueError('公告单号必须是文本')
    key = (key or '').strip().upper()
    if key and not KEY.fullmatch(key):
        raise ValueError('公告单号格式应为 TRANSACT-210390 这种')
    return key


def compose(row, params, runtime, base_url, test=False):
    """Subject and plain-text body for one successful build."""
    app = runtime.get('testfairy_app') or {}
    link = app.get('share_url', '')
    build = runtime.get('gmx_build_number', runtime.get('gmx_formal_number', ''))
    country, project = params.get('country', ''), params.get('project', '')
    subject = f'{"[TEST] " if test else ""}[新测试包] {country} {project} · GMX #{build}'.replace('  ', ' ')
    lines = []
    if test:
        lines += ['这是一封测试邮件（内容取自一次已成功的历史构建），不代表有新包。', '']
    lines += ['新的测试包已构建完成。', '',
              f'App 链接：{link}',
              f'Plugin 分支：{params.get("plugin_source", "")} → {params.get("plugin_branch", "")}',
              f'GMX 构建：#{build}' + (f'  {app["build_url"]}' if app.get('build_url') else ''),
              f'构建任务：{row["id"]}', '']
    report = runtime.get('jira_report') or {}
    status = report.get('status')
    browse = (base_url or '').rstrip('/') + '/browse/'
    if status in ('DONE', 'PARTIAL', 'DRY_RUN'):
        tickets = report.get('tickets') or []
        head = '本次更新的 Jira' + ('（演练模式，实际未修改）' if status == 'DRY_RUN' else '') + '：'
        lines.append(head if tickets else '本次没有需要更新的 Jira。')
        for t in tickets:
            title = f' {t["summary"]}' if t.get('summary') else ''
            lines.append(f'- {t["key"]}{title}  {browse}{t["key"]}')
        for e in report.get('errors') or []:
            lines.append(f'- {e["key"]}（自动更新失败，请手动处理）  {browse}{e["key"]}')
        for m in report.get('needs_manual') or []:
            lines.append(f'  注意：{m["key"]} 状态需手动修改')
    elif status == 'BASELINE':
        lines.append('Jira：首次运行，只记录起点，下次构建开始自动更新。')
    else:
        lines.append('Jira：本次未自动更新。')
    lines += ['', '—— 由 APP Builder 自动发送']
    return safe_text(subject)[:250], '\n'.join(lines)


def request_for(issue, users, subject, body, flavor='server'):
    who = [{'accountId': u} if flavor == 'cloud' else {'name': u} for u in users]
    payload = {'subject': subject, 'textBody': body,
               'to': {'reporter': False, 'assignee': False, 'watchers': False, 'voters': False, 'users': who}}
    return f'/rest/api/2/issue/{quote(issue)}/notify', payload


def send(client, issue, users, subject, body, flavor='server'):
    path, payload = request_for(issue, users, subject, body, flavor)
    return client._call('POST', path, payload)


def describe_request(base_url, issue, users, subject, body, flavor='server'):
    """The exact request, for the page: method, URL and JSON body (no token)."""
    path, payload = request_for(issue, users, subject, body, flavor)
    return 'POST ' + base_url.rstrip('/') + path + '\n' + json.dumps(payload, ensure_ascii=False, indent=2)


def _client(conf):
    from .jira_notify import JiraClient
    return JiraClient(conf.get('base_url', ''), flavor=conf['flavor'], timeout=conf['timeout_seconds'],
                      ca_bundle=conf.get('ca_bundle'))


def notify_team(task_id, client=None, config=None):
    """After notify_jira: send the one announcement email for this successful build."""
    from .jira_notify import settings
    row = task(task_id)
    if row['status'] != 'SUCCESS':
        return
    conf = settings(config or load_config())
    if not conf.get('mail_enabled'):
        return
    runtime = read_state(task_id).get('runtime', {})
    if (runtime.get('jira_mail') or {}).get('status') == 'SENT':
        return
    link = (runtime.get('testfairy_app') or {}).get('share_url')

    def record(value):
        patch_runtime(task_id, jira_mail=value)
        action('MAIL', safe_text(value.get('reason') or f'已通过 Jira 发送给 {len(value.get("to", []))} 人'))

    try:
        issue = check_issue(conf.get('mail_issue', ''))
        users = parse_users(conf.get('mail_users', ''))
    except ValueError as exc:
        return record({'status': 'SKIPPED', 'reason': '邮件设置有误：' + str(exc)})
    if not (isinstance(link, str) and link.startswith('https://')):
        return record({'status': 'SKIPPED', 'reason': '本次没有 App 链接，不发邮件'})
    if not issue or not users or not conf.get('base_url'):
        return record({'status': 'SKIPPED', 'reason': '未填写 Jira 地址、公告单号或收件人，不发邮件'})
    params = json.loads(row['params'])
    subject, body = compose(row, params, runtime, conf['base_url'])
    try:
        send(client or _client(conf), issue, users, subject, body, conf['flavor'])
    except Exception as exc:
        return record({'status': 'FAILED', 'issue': issue, 'to': users, 'subject': subject,
                       'reason': '邮件发送失败（不会自动重发）：' + safe_text(str(exc))[:300]})
    record({'status': 'SENT', 'issue': issue, 'to': users, 'subject': subject})


def latest_success():
    """Most recent SUCCESS task that has an App link, for the test email."""
    from .store import connect
    db = connect()
    try:
        rows = db.execute("SELECT id FROM tasks WHERE status='SUCCESS' ORDER BY created DESC LIMIT 50").fetchall()
    finally:
        db.close()
    for r in rows:
        runtime = read_state(r['id']).get('runtime', {})
        if str((runtime.get('testfairy_app') or {}).get('share_url', '')).startswith('https://'):
            return r['id']
    return None


def test_email(body, client=None, config=None):
    """Send one [TEST] email now to the users typed on the page (or the saved ones)."""
    from .jira_notify import settings
    conf = settings(config or load_config())
    if not conf.get('base_url'):
        raise ValueError('请先填写并保存 Jira 地址')
    issue = check_issue(body.get('mail_issue', conf.get('mail_issue', '')))
    users = parse_users(body.get('mail_users', conf.get('mail_users', '')))
    if not issue:
        raise ValueError('请填写公告单号')
    if not users:
        raise ValueError('请至少填写一个收件人的 Jira 用户名')
    task_id = latest_success()
    if task_id:
        row = task(task_id)
        subject, text = compose(row, json.loads(row['params']), read_state(task_id).get('runtime', {}),
                                conf['base_url'], test=True)
    else:
        subject = '[TEST] APP Builder 邮件测试'
        text = '这是一封测试邮件。还没有带 App 链接的成功构建，所以没有附构建内容。\n\n—— 由 APP Builder 自动发送'
    request = describe_request(conf['base_url'], issue, users, subject, text, conf['flavor'])
    try:
        send(client or _client(conf), issue, users, subject, text, conf['flavor'])
    except Exception as exc:
        message = safe_text(str(exc))
        hint = ''
        if 'No recipients' in message:
            hint = ('（Jira 不会给发起操作的人本人发通知：收件人只有你自己时就等于没人。'
                    '请至少再加一位同事的用户名）')
        elif 'HTTP 400' in message:
            hint = '（常见原因：用户名不存在，或公告单号不对）'
        elif 'HTTP 403' in message or 'HTTP 401' in message:
            hint = '（Token 无效，或公司 Jira 不允许用接口发通知）'
        elif 'HTTP 404' in message:
            hint = '（公告单不存在，或 Token 看不到这张单）'
        raise ValueError('发送失败：' + message + hint + '\n\n本次请求（不含 Token）：\n' + request) from None
    return {'ok': True, 'issue': issue, 'to': users, 'subject': subject, 'from_task': task_id, 'request': request}
