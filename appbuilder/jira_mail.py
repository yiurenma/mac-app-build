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
# Emails and Jira comments are read by the whole team: English only.
STAGE_NAMES = {'INIT': 'start', 'PLUGIN_PREPARE': 'Plugin preparation', 'PLUGIN_CHECKOUT': 'Plugin preparation',
               'PLUGIN_EDIT': 'Plugin preparation', 'PLUGIN_PUSH': 'Plugin push', 'PLUGIN_JENKINS': 'Plugin build',
               'GMX_CHECKOUT': 'GMX preparation', 'GMX_EDIT': 'GMX preparation', 'GMX_LOCK': 'GMX preparation',
               'GMX_PUSH': 'GMX push', 'GMX_JENKINS': 'GMX build', 'TESTFAIRY_LOOKUP': 'App link lookup'}
CJK = re.compile(r'[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]')


def english_only(text):
    """True when the text has no Chinese characters (internal log lines are Chinese)."""
    return not CJK.search(text or '')


# The tool's own failure reasons are Chinese (for the page). The email needs English, so the
# common kinds of reason are mapped to a plain English sentence; numbers and Jenkins results
# are carried over. Order matters: the first match wins.
REASONS = [
    (r'(?:正式构建|Build) #(\d+) 失败: ([A-Z_]+)', 'Jenkins build #{0} finished with result {1}.'),
    (r'Callback #(\d+) 失败: ([A-Z_]+)', 'The Plugin callback build #{0} finished with result {1}.'),
    (r'exit=(\d+)', 'A local step failed (exit code {0}); see the task log in APP Builder.'),
    (r'TestFairy', 'The App link could not be found on TestFairy (session expired or the upload is not there yet).'),
    (r'登录|SSO|MFA|认证|会话已过期|密码', 'Jenkins sign-in is needed (the session expired or SSO was not completed).'),
    (r'必填|校验未通过|BUILD_TYPE|参数', 'Jenkins did not accept the build parameters, so nothing was started.'),
    (r'队列|Build Number|新 Build|排队|回执', 'APP Builder could not confirm which Jenkins build belongs to this run, so it did not submit again.'),
    (r'超时', 'Timed out waiting for Jenkins; the build may still be running.'),
    (r'冲突|合并|未暂存|工作区|外部提交|外部修改|远端|基线|分支', 'The Plugin branch changed or has conflicts and needs a manual check before building.'),
    (r'lock', 'Updating the GMX lock file produced unexpected changes, so nothing was pushed.'),
    (r'Worker|进程', 'APP Builder stopped before the build finished (for example the Mac slept or restarted).'),
]


def english_reason(note):
    """English for a failure note: as is when already English, else the matching sentence."""
    note = safe_text(note or '')[:500]
    if not note:
        return ''
    if english_only(note):
        return note
    for pattern, text in REASONS:
        m = re.search(pattern, note)
        if m:
            return text.format(*m.groups())
    return ''


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


def parse_groups(text):
    """Jira group names, one per line or comma separated (group names may contain spaces)."""
    if not isinstance(text, str):
        raise ValueError('用户组必须是文本')
    names = [n.strip() for n in re.split(r'[\n,;，；]+', text) if n.strip()]
    bad = [n for n in names if len(n) > 255 or re.search(r'[<>"\\]', n)]
    if bad:
        raise ValueError('用户组名称格式不对：' + '、'.join(bad[:5]))
    names = list(dict.fromkeys(names))
    if len(names) > 10:
        raise ValueError('用户组最多 10 个')
    return names


def _when(row=None):
    return datetime.now().strftime('%d %b %Y, %H:%M')


def _title(params):
    return ' '.join(x for x in (params.get('country', ''), params.get('project', '')) if x)


def _ticket_rows(runtime, base_url):
    """[(key, url, summary, why, chip_text, chip_kind)] for the tickets this build touched."""
    report = runtime.get('jira_report') or {}
    browse = (base_url or '').rstrip('/') + '/browse/'
    name = report.get('transition') or 'In Test'
    manual = {m['key'] for m in report.get('needs_manual') or []}
    rows = []
    for t in report.get('tickets') or []:
        items = t.get('ref_items')
        if items is None:                                      # reports written before ref_items
            items = [{'label': re.sub(r'^- ', '', r).split(' "')[0], 'author': ''} for r in t.get('refs') or []]
        why = '; '.join(i['label'] + (f' · {i["author"]}' if i.get('author') else '') for i in items)
        if t.get('assignee'):
            why += (' · ' if why else '') + 'assigned to ' + t['assignee']
        if report.get('status') == 'DRY_RUN':
            chip = ('Rehearsal, not changed', 'grey')
        elif t['key'] in manual:
            chip = (f'Move to {name} manually', 'warn')
        else:
            chip = (t.get('status') or name, 'ok')
        rows.append((t['key'], browse + t['key'], t.get('summary') or '', why, *chip, t.get('tester_note') or ''))
    for e in report.get('errors') or []:
        rows.append((e['key'], browse + e['key'], '', 'Could not be updated automatically', 'Please check', 'bad', ''))
    return rows


def _jira_line(runtime):
    """One sentence when there is no ticket list to show (or None when there is one)."""
    report = runtime.get('jira_report') or {}
    status = report.get('status')
    if status in ('DONE', 'PARTIAL', 'DRY_RUN'):
        return None if (report.get('tickets') or report.get('errors')) else 'No new tickets since the last build.'
    if status == 'BASELINE':
        return 'Jira: first run, only the starting point was recorded. Tickets are updated from the next build.'
    return 'Jira was not updated for this build.'


def compose(row, params, runtime, base_url, test=False, recovered=False):
    """Subject and plain-text body (English) for one successful build."""
    app = runtime.get('testfairy_app') or {}
    link = app.get('share_url', '')
    build = runtime.get('gmx_build_number', runtime.get('gmx_formal_number', ''))
    branch = params.get('plugin_branch', '')
    rows = _ticket_rows(runtime, base_url)
    count = f' · {len(rows)} ticket' + ('s' if len(rows) != 1 else '') if rows else ''
    subject = f'{"[TEST] " if test else ""}Ready to test: {_title(params)} {branch} · GMX #{build}{count}'
    lines = []
    if test:
        lines += ['This is a test email (content from an earlier successful build). It is not a new build.', '']
    if recovered:
        lines += ['The earlier build failure has recovered.', '']
    lines += [f'Ready to test: {_title(params)} · {branch} · GMX #{build}', f'Install from TestFairy: {link}', '']
    line = _jira_line(runtime)
    if line:
        lines.append(line)
    else:
        lines.append(f'Tickets in this build ({len(rows)}):')
        for key, url, summary, why, chip, _, note in rows:
            lines.append(f'- {key} {summary}  [{chip}]  {url}')
            if note:
                lines.append(f'    For testers: {note}')
            if why:
                lines.append(f'    {why}')
    lines += ['', f'Plugin {params.get("plugin_source", "")} -> {branch}'
              + (f' · Jenkins GMX #{build} {app["build_url"]}' if app.get('build_url') else '')
              + f' · task {row["id"]}', '', '-- sent automatically by APP Builder']
    return safe_text(' '.join(subject.split()))[:250], '\n'.join(lines)


# ---- HTML email -------------------------------------------------------------------------
# Inline styles and tables only: Outlook and BlackBerry Work drop <style> blocks.
GREEN, GREEN_BG, RED, RED_BG = '#1f7a45', '#e9f5ee', '#b42318', '#fdecea'
AMBER, AMBER_BG, GREY, GREY_BG, INK, LINE, LINK = '#9a5b00', '#fff4e0', '#6b778c', '#f4f5f7', '#172b4d', '#dfe1e6', '#0052cc'
FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def _e(text):
    import html
    return html.escape(str(text or ''), quote=True)


def _a(url, text, style=''):
    if not (isinstance(url, str) and url.startswith('https://')):
        return _e(text)
    return f'<a href="{_e(url)}" style="color:{LINK};text-decoration:none;{style}">{_e(text)}</a>'


def _button(url, text, color):
    return (f'<a href="{_e(url)}" style="display:inline-block;margin-top:12px;padding:9px 16px;border-radius:4px;'
            f'background:{color};color:#ffffff;font-weight:600;font-size:14px;text-decoration:none">{_e(text)}</a>')


def _band(ok, state, title, meta, button):
    color, bg = (GREEN, GREEN_BG) if ok else (RED, RED_BG)
    return (f'<tr><td style="background:{bg};padding:14px 16px;border-bottom:1px solid {LINE}">'
            f'<div style="font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:{color}">{_e(state)}</div>'
            f'<div style="font-size:18px;font-weight:600;color:{INK};margin-top:2px">{_e(title)}</div>'
            f'<div style="font-size:13px;color:{GREY};margin-top:2px">{_e(meta)}</div>{button}</td></tr>')


def _section(inner, head=''):
    h = (f'<div style="font-size:12px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:{GREY};'
         f'margin-bottom:6px">{_e(head)}</div>') if head else ''
    return f'<tr><td style="padding:12px 16px;border-top:1px solid {LINE}">{h}{inner}</td></tr>'


def _footer(parts):
    return (f'<tr><td style="padding:10px 16px;border-top:1px solid {LINE};font-size:12px;color:{GREY}">'
            + ' · '.join(parts) + '</td></tr>')


def _frame(rows, note=''):
    top = (f'<p style="margin:0 0 10px;font-family:{FONT};font-size:13px;color:{GREY}">{_e(note)}</p>') if note else ''
    return (f'<div style="font-family:{FONT};color:{INK};font-size:14px;line-height:1.5">{top}'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="max-width:640px;border:1px solid {LINE};border-radius:6px;border-collapse:separate;font-family:{FONT}">'
            + ''.join(rows) + '</table></div>')


def _chip(text, kind):
    color, bg = {'ok': (GREEN, GREEN_BG), 'warn': (AMBER, AMBER_BG), 'bad': (RED, RED_BG)}.get(kind, (GREY, GREY_BG))
    return (f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;'
            f'white-space:nowrap;background:{bg};color:{color}">{_e(text)}</span>')


def success_html(row, params, runtime, base_url, test=False, recovered=False):
    app = runtime.get('testfairy_app') or {}
    build = runtime.get('gmx_build_number', runtime.get('gmx_formal_number', ''))
    branch = params.get('plugin_branch', '')
    meta = f'GMX #{build} · {_when()}' + (' · recovered from the earlier failure' if recovered else '')
    out = [_band(True, 'Ready to test', f'{_title(params)} · {branch}', meta,
                 _button(app.get('share_url', ''), 'Install from TestFairy', GREEN))]
    line = _jira_line(runtime)
    if line:
        out.append(_section(f'<span style="color:{GREY}">{_e(line)}</span>'))
    else:
        rows = _ticket_rows(runtime, base_url)
        cells = []
        for i, (key, url, summary, why, chip, kind, note) in enumerate(rows):
            border = f'border-top:1px solid {LINE};' if i else ''
            # Two rows per ticket (key + state, then the text) so it also reads on a phone.
            cells.append(
                f'<tr><td valign="top" style="{border}padding:10px 8px 2px 0;font-weight:600">{_a(url, key)}</td>'
                f'<td valign="top" align="right" style="{border}padding:10px 0 2px">{_chip(chip, kind)}</td></tr>'
                f'<tr><td colspan="2" style="padding:0 0 10px">{_e(summary)}'
                + (f'<div style="margin-top:3px;font-size:13.5px"><b>For testers:</b> {_e(note)}</div>' if note else '')
                + (f'<div style="font-size:12.5px;color:{GREY};margin-top:2px">{_e(why)}</div>' if why else '')
                + '</td></tr>')
        table = ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
                 f'style="border-collapse:collapse;font-size:14px">' + ''.join(cells) + '</table>')
        out.append(_section(table, f'Tickets in this build ({len(rows)})'))
    foot = [f'Plugin {_e(params.get("plugin_source", ""))} → {_e(branch)}']
    if app.get('build_url'):
        foot.append(_a(app['build_url'], f'Jenkins GMX #{build}'))
    foot.append(f'task {_e(row["id"])}')
    out.append(_footer(foot))
    return _frame(out, 'This is a test email (content from an earlier successful build). It is not a new build.' if test else '')


def stage_name(stage):
    return STAGE_NAMES.get(stage or '', stage or 'unknown step')


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


def _failure_parts(row, links):
    reason = english_reason(row.get('note'))
    tail = [line for line in _log_tail(row['id']) if english_only(line)]
    return reason, tail, links or []


def _hint(row, reason, tail):
    """Optional offline-AI suggestion (cause + next action). Never blocks the email."""
    try:
        from .ai_setup import feature_on
        config = load_config()
        if not feature_on(config, 'failure_hint'):
            return None
        from .local_llm import get_llm, failure_hint
        return failure_hint(get_llm(config), stage_name(row.get('stage')), reason, tail)
    except Exception:
        return None


def compose_failure(row, params, runtime, links=None, test=False, hint=None):
    """Subject and plain-text body (English) for a failed build: where, and where to look.
    The tool's own reason and log lines are Chinese, so only English lines are quoted."""
    where = stage_name(row.get('stage'))
    branch = params.get('plugin_branch', '')
    subject = f'{"[TEST] " if test else ""}Build failed: {_title(params)} {branch} · at {where}'
    reason, tail, links = _failure_parts(row, links)
    lines = (['This is a test email (content from an earlier failed build).', ''] if test else []) + [
        f'Build failed: stopped at {where} ({row.get("status", "")})', f'{_title(params)} · {branch}']
    if reason:
        lines.append(f'Reason: {reason}')
    if hint:
        lines += [f'Likely cause (offline AI suggestion): {hint["cause"]}', f'Try: {hint["fix"]}']
    if links:
        lines += [''] + [f'{item["label"]}: {item["url"]}' for item in links]
    if tail:
        lines += ['', 'Last log lines:'] + [f'  {line}' for line in tail]
    lines += ['', 'After fixing it, press Resume on the task in APP Builder. The next scheduled build will also try again.',
              '', f'Plugin {params.get("plugin_source", "")} -> {branch} · task {row["id"]}',
              '', '-- sent automatically by APP Builder']
    return safe_text(' '.join(subject.split()))[:250], '\n'.join(lines)


def failure_html(row, params, runtime, links=None, test=False, hint=None):
    where = stage_name(row.get('stage'))
    branch = params.get('plugin_branch', '')
    reason, tail, links = _failure_parts(row, links)
    button = _button(links[0]['url'], 'Open Jenkins log', RED) if links else ''
    out = [_band(False, 'Build failed', f'Stopped at {where}', f'{_title(params)} · {branch} · {_when()}', button)]
    if reason:
        out.append(_section(_e(reason), 'Reason'))
    if hint:
        out.append(_section(f'{_e(hint["cause"])}<div style="margin-top:4px"><b>Try:</b> {_e(hint["fix"])}</div>',
                            'Likely cause · offline AI suggestion'))
    if tail:
        box = (f'<div style="background:{GREY_BG};border:1px solid {LINE};border-radius:4px;padding:8px 10px;'
               f"font-family:Menlo,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word\">"
               + '<br>'.join(_e(t) for t in tail) + '</div>')
        out.append(_section(box, 'Last log lines'))
    out.append(_section('After fixing it, press <b>Resume</b> on the task in APP Builder. '
                        'The next scheduled build will also try again.'))
    foot = [f'Plugin {_e(params.get("plugin_source", ""))} → {_e(branch)}']
    foot += [_a(item['url'], item['label']) for item in links[1:]]
    foot.append(f'task {_e(row["id"])}')
    out.append(_footer(foot))
    return _frame(out, 'This is a test email (content from an earlier failed build).' if test else '')


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


def request_for(issue, users, subject, body, flavor='server', html=None, groups=()):
    who = [{'accountId': u} if flavor == 'cloud' else {'name': u} for u in users]
    to = {'reporter': False, 'assignee': False, 'watchers': False, 'voters': False, 'users': who}
    if groups:
        to['groups'] = [{'name': g} for g in groups]
    payload = {'subject': subject, 'textBody': body, 'htmlBody': html or html_body(body), 'to': to}
    return f'/rest/api/2/issue/{quote(issue)}/notify', payload


def send(client, issue, users, subject, body, flavor='server', html=None, groups=()):
    path, payload = request_for(issue, users, subject, body, flavor, html, groups)
    return client._call('POST', path, payload)


def describe_request(base_url, issue, users, subject, body, flavor='server', html=None, groups=()):
    """The exact request, for the page: method, URL and JSON body (no token)."""
    path, payload = request_for(issue, users, subject, body, flavor, html, groups)
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
        groups = parse_groups(conf.get('mail_groups', ''))
    except ValueError as exc:
        return record({'status': 'SKIPPED', 'kind': kind, 'reason': '邮件设置有误：' + str(exc)})
    if not issue or not (users or groups) or not conf.get('base_url'):
        return record({'status': 'SKIPPED', 'kind': kind, 'reason': '未填写 Jira 地址、公告单号或收件人，不发邮件'})
    key = _failure_key(params)
    recovered = False
    if kind == 'success':
        link = (runtime.get('testfairy_app') or {}).get('share_url')
        if not (isinstance(link, str) and link.startswith('https://')):
            return record({'status': 'SKIPPED', 'kind': kind, 'reason': '本次没有 App 链接，不发邮件'})
        recovered = bool(_mail_state().get(key))
        subject, body = compose(row, params, runtime, conf['base_url'], recovered=recovered)
        html = success_html(row, params, runtime, conf['base_url'], recovered=recovered)
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
        reason, tail, _ = _failure_parts(row, links)
        hint = _hint(row, reason, tail)
        subject, body = compose_failure(row, params, runtime, links, hint=hint)
        html = failure_html(row, params, runtime, links, hint=hint)
    to = users + [f'group:{g}' for g in groups]
    entry = {'kind': kind, 'task_id': task_id, 'issue': issue, 'to': to, 'subject': subject, 'body': body}
    try:
        send(client or _client(conf), issue, users, subject, body, conf['flavor'], html, groups)
    except Exception as exc:
        reason = '邮件发送失败（不会自动重发）：' + safe_text(str(exc))[:300]
        log_mail({**entry, 'status': 'FAILED', 'reason': reason})
        return record({'status': 'FAILED', 'kind': kind, 'issue': issue, 'to': to, 'subject': subject,
                       'reason': reason})
    if kind == 'failure':
        _mail_state(lambda data: {**data, key: _signature(row)})
    elif recovered:
        _mail_state(lambda data: {k: v for k, v in data.items() if k != key})
    log_mail({**entry, 'status': 'SENT'})
    record({'status': 'SENT', 'kind': kind, 'issue': issue, 'to': to, 'subject': subject,
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
    groups = parse_groups(body.get('mail_groups', conf.get('mail_groups', '')))
    if not issue:
        raise ValueError('请填写公告单号')
    if not users and not groups:
        raise ValueError('请至少填写一个收件人的 Jira 用户名或一个用户组')
    task_id = latest_success()
    html = None
    if task_id:
        row = task(task_id)
        params, runtime = json.loads(row['params']), read_state(task_id).get('runtime', {})
        subject, text = compose(row, params, runtime, conf['base_url'], test=True)
        html = success_html(row, params, runtime, conf['base_url'], test=True)
    else:
        subject = '[TEST] APP Builder email test'
        text = ('This is a test email. There is no successful build with an App link yet, so no build '
                'details are included.\n\n-- sent automatically by APP Builder')
    request = describe_request(conf['base_url'], issue, users, subject, text, conf['flavor'], html, groups)
    to = users + [f'group:{g}' for g in groups]
    entry = {'kind': 'test', 'task_id': task_id, 'issue': issue, 'to': to, 'subject': subject, 'body': text}
    try:
        send(client or _client(conf), issue, users, subject, text, conf['flavor'], html, groups)
    except Exception as exc:
        message = safe_text(str(exc))
        log_mail({**entry, 'status': 'FAILED', 'reason': message[:300]})
        hint = ''
        if 'No recipients' in message:
            hint = ('（Jira 不会给发起操作的人本人发通知：收件人只有你自己时就等于没人。'
                    '请至少再加一位同事的用户名）')
        elif 'HTTP 400' in message:
            hint = '（常见原因：用户名或用户组不存在，或公告单号不对）'
        elif 'HTTP 403' in message or 'HTTP 401' in message:
            hint = '（Token 无效，或公司 Jira 不允许用接口发通知）'
        elif 'HTTP 404' in message:
            hint = '（公告单不存在，或 Token 看不到这张单）'
        raise ValueError('发送失败：' + message + hint + '\n\n本次请求（不含 Token）：\n' + request) from None
    log_mail({**entry, 'status': 'SENT'})
    return {'ok': True, 'issue': issue, 'to': to, 'subject': subject, 'from_task': task_id, 'request': request}
