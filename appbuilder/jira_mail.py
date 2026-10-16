"""Email the team through Jira when a build finishes.

Jira Server / Data Center has a REST call that sends an email about an issue to chosen
users: POST /rest/api/2/issue/{key}/notify. The Jira server sends the mail, so no
Outlook, mail account or password is involved; the saved Jira token is enough.

The call must name an issue, so the team keeps one fixed "announcement" ticket and every
email goes out through it. One email per finished build: a successful build with an App
link gets the "new test package" email, a failed one (FAILED / NEEDS_ATTENTION / TIMEOUT)
gets a failure email. With mail_failure_once (default on) the same failure reason repeating
on the same branches is reported once until a build succeeds again. Every email is written
to a local log (mail_log.jsonl) for the 交付记录 page. A failure here never changes the
build result, and nothing is retried automatically.
"""
from __future__ import annotations
import json
import re
from urllib.parse import quote

import fcntl
import os
from datetime import datetime

from .config import DATA, load_config
from .store import task, read_state, patch_runtime, task_folder
from .telemetry import action, safe_text

USER = re.compile(r'[A-Za-z0-9._@+-]{1,128}')
KEY = re.compile(r'[A-Z][A-Z0-9]{1,9}-\d{1,7}')
MAX_USERS = 50
MAIL_LOG = DATA / 'mail_log.jsonl'
MAIL_STATE = DATA / 'mail_state.json'
FAILED = ('FAILED', 'NEEDS_ATTENTION', 'TIMEOUT')
STAGE_NAMES = {'INIT': '启动', 'PLUGIN_PREPARE': 'Plugin 代码准备', 'PLUGIN_CHECKOUT': 'Plugin 代码准备',
               'PLUGIN_EDIT': 'Plugin 代码准备', 'PLUGIN_PUSH': 'Plugin 推送', 'PLUGIN_JENKINS': 'Plugin 构建',
               'GMX_CHECKOUT': 'GMX 代码准备', 'GMX_EDIT': 'GMX 代码准备', 'GMX_LOCK': 'GMX 代码准备',
               'GMX_PUSH': 'GMX 推送', 'GMX_JENKINS': 'GMX 构建', 'TESTFAIRY_LOOKUP': '取 App 链接'}


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


def stage_name(stage):
    return STAGE_NAMES.get(stage or '', stage or '未知步骤')


def _log_tail(task_id, keep=8):
    """The last few lines that explain a failure; scrubbed like every other log view."""
    try:
        path = task_folder(task_id) / 'run.log'
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 12000))
            text = stream.read().decode('utf-8', 'replace')
    except OSError:
        return []
    hits = [line.strip() for line in text.splitlines()
            if re.search(r'\[(?:FAILED|NEEDS_ATTENTION|TIMEOUT|ERROR)\]|失败|错误|Error|Exception', line)]
    return [safe_text(line)[:300] for line in hits[-keep:]]


def compose_failure(row, params, runtime, links=None, test=False):
    """Subject and plain-text body for a failed build: where, why, and where to look."""
    country, project = params.get('country', ''), params.get('project', '')
    where = stage_name(row.get('stage'))
    subject = f'{"[TEST] " if test else ""}[构建失败] {country} {project} · 失败在 {where}'.replace('  ', ' ')
    lines = (['这是一封测试邮件（内容取自一次失败的历史构建）。', ''] if test else []) + [
        '构建没有完成，需要处理。', '',
        f'失败步骤：{where}',
        f'结果：{row.get("status", "")}',
        f'原因：{safe_text(row.get("note") or "未记录")[:500]}',
        f'Plugin 分支：{params.get("plugin_source", "")} → {params.get("plugin_branch", "")}',
        f'构建任务：{row["id"]}']
    links = links or []
    if links:
        lines += ['', 'Jenkins：'] + [f'- {item["label"]}  {item["url"]}' for item in links]
    tail = _log_tail(row['id'])
    if tail:
        lines += ['', '日志最后几行：'] + [f'  {line}' for line in tail]
    lines += ['', '处理后可在 App Builder 的运行记录里点「从失败处继续」；定时任务下次也会再跑。',
              '', '—— 由 APP Builder 自动发送']
    return safe_text(subject)[:250], '\n'.join(lines)


def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path.with_suffix('.lock'), 'a+')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def log_mail(entry):
    """Append one email to the local log shown on the 交付记录 page."""
    entry = {'at': datetime.now().isoformat(timespec='seconds'), **entry}
    handle = _locked(MAIL_LOG)
    try:
        with MAIL_LOG.open('a', encoding='utf-8') as out:
            out.write(json.dumps(entry, ensure_ascii=False) + '\n')
    finally:
        handle.close()
    return entry


def read_mails(query='', limit=300):
    try:
        lines = MAIL_LOG.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    rows = []
    q = (query or '').strip().lower()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if q and q not in ' '.join(str(row.get(k, '')) for k in ('subject', 'task_id', 'body', 'to')).lower():
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _failure_key(params):
    return '|'.join(str(params.get(k, '')) for k in ('plugin_source', 'gmx_source', 'country', 'project'))


def _signature(row):
    return f'{row.get("stage")}|{re.sub(r"[0-9]+", "#", safe_text(row.get("note") or ""))[:200]}'


def _mail_state(update=None):
    """Last reported failure per branch set. update(dict) -> new dict, saved atomically."""
    handle = _locked(MAIL_STATE)
    try:
        try:
            data = json.loads(MAIL_STATE.read_text(encoding='utf-8'))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        if update is not None:
            data = update(data)
            tmp = MAIL_STATE.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, MAIL_STATE)
        return data
    finally:
        handle.close()


def html_body(text):
    """HTML version of the plain body: Jira sends HTML mail to most users and ignores
    textBody for them, so without this the email arrives with no content."""
    import html
    out = []
    for line in text.split('\n'):
        escaped = html.escape(line)
        escaped = re.sub(r'https://[^\s<>&]+(?:&amp;[^\s<>&]+)*',
                         lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
        out.append(escaped)
    return '<div style="font-family:sans-serif;font-size:14px;line-height:1.5">' + '<br>'.join(out) + '</div>'


def request_for(issue, users, subject, body, flavor='server'):
    who = [{'accountId': u} if flavor == 'cloud' else {'name': u} for u in users]
    payload = {'subject': subject, 'textBody': body, 'htmlBody': html_body(body),
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
    """After notify_jira: one email for this finished build (success or failure)."""
    from .jira_notify import settings
    row = task(task_id)
    status = row['status']
    if status != 'SUCCESS' and status not in FAILED:
        return
    conf = settings(config or load_config())
    if not conf.get('mail_enabled'):
        return
    runtime = read_state(task_id).get('runtime', {})
    params = json.loads(row['params'])
    kind = 'success' if status == 'SUCCESS' else 'failure'
    previous = runtime.get('jira_mail') or {}
    # One email per OUTCOME of this task: a failure email must not block the success email
    # after Resume, and a resumed task failing for a new reason is reported again.
    if previous.get('status') in ('SENT', 'SUPPRESSED') and previous.get('kind', 'failure') == kind \
            and (kind == 'success' or previous.get('signature') == _signature(row)):
        return

    def record(value):
        patch_runtime(task_id, jira_mail=value)
        action('MAIL', safe_text(value.get('reason') or f'已通过 Jira 发送给 {len(value.get("to", []))} 人'))

    try:
        issue = check_issue(conf.get('mail_issue', ''))
        users = parse_users(conf.get('mail_users', ''))
    except ValueError as exc:
        return record({'status': 'SKIPPED', 'kind': kind, 'reason': '邮件设置有误：' + str(exc)})
    if not issue or not users or not conf.get('base_url'):
        return record({'status': 'SKIPPED', 'kind': kind, 'reason': '未填写 Jira 地址、公告单号或收件人，不发邮件'})
    key = _failure_key(params)
    recovered = False
    if kind == 'success':
        link = (runtime.get('testfairy_app') or {}).get('share_url')
        if not (isinstance(link, str) and link.startswith('https://')):
            return record({'status': 'SKIPPED', 'kind': kind, 'reason': '本次没有 App 链接，不发邮件'})
        subject, body = compose(row, params, runtime, conf['base_url'])
        recovered = bool(_mail_state().get(key))
        if recovered:
            body = '之前的构建失败已恢复。\n\n' + body
    else:
        signature = _signature(row)
        if conf.get('mail_failure_once', True) and _mail_state().get(key) == signature:
            return record({'status': 'SUPPRESSED', 'kind': kind, 'signature': signature,
                           'reason': '同一原因的失败已经通知过，恢复成功前不再重复发'})
        from .web import _jenkins_links
        try:
            links = [item for item in _jenkins_links(row, params) if '#' in item['label']]
        except Exception:
            links = []
        subject, body = compose_failure(row, params, runtime, links)
    entry = {'kind': kind, 'task_id': task_id, 'issue': issue, 'to': users, 'subject': subject, 'body': body}
    try:
        send(client or _client(conf), issue, users, subject, body, conf['flavor'])
    except Exception as exc:
        reason = '邮件发送失败（不会自动重发）：' + safe_text(str(exc))[:300]
        log_mail({**entry, 'status': 'FAILED', 'reason': reason})
        return record({'status': 'FAILED', 'kind': kind, 'issue': issue, 'to': users, 'subject': subject,
                       'reason': reason})
    if kind == 'failure':
        _mail_state(lambda data: {**data, key: _signature(row)})
    elif recovered:
        _mail_state(lambda data: {k: v for k, v in data.items() if k != key})
    log_mail({**entry, 'status': 'SENT'})
    record({'status': 'SENT', 'kind': kind, 'issue': issue, 'to': users, 'subject': subject,
            'signature': _signature(row) if kind == 'failure' else None})


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
    entry = {'kind': 'test', 'task_id': task_id, 'issue': issue, 'to': users, 'subject': subject, 'body': text}
    try:
        send(client or _client(conf), issue, users, subject, text, conf['flavor'])
    except Exception as exc:
        message = safe_text(str(exc))
        log_mail({**entry, 'status': 'FAILED', 'reason': message[:300]})
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
    log_mail({**entry, 'status': 'SENT'})
    return {'ok': True, 'issue': issue, 'to': users, 'subject': subject, 'from_task': task_id, 'request': request}
