"""After a successful build: write the app link to each Jira ticket that landed in the
source branch since the last processed build, assign it to its Reporter and move it to
the configured status (default "In Test").

Deterministic rules do the work. The optional offline model only (a) picks one ticket when
a commit names several and (b) rewrites a commit into a short tester-facing note.
Default is OFF and dry-run. A failure here never changes the build result.
"""
from __future__ import annotations
import base64
import json
import os
import re
import subprocess
from datetime import datetime
import urllib.error
import urllib.request
from urllib.parse import urlsplit, quote
from .config import DATA, load_config
from .store import task, read_state, patch_runtime, task_folder
from .telemetry import action, safe_text
from .local_llm import get_llm, pick_primary_ticket, tester_summary

DEFAULT_KEY = r'\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b'
LEDGER = DATA / 'jira_ledger.json'
SETTINGS_FILE = DATA / 'jira_settings.json'
EDITABLE = {'enabled': bool, 'dry_run': bool, 'base_url': str, 'transition_name': str, 'assign_to_reporter': bool,
            'use_pr_lookup': bool, 'key_pattern': str, 'first_run_commits': int, 'max_tickets_per_run': int,
            'flavor': str, 'transition_fields': str,
            'mail_enabled': bool, 'mail_issue': str, 'mail_users': str, 'mail_failure_once': bool,
            'comment_mentions': str, 'mail_groups': str}


def _type_ok(key, value):
    kind = EDITABLE[key]
    return isinstance(value, kind) and not (kind is int and isinstance(value, bool))


def saved_settings():
    """Settings saved from the web page; they override config.yaml."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE and _type_ok(k, v)} if isinstance(data, dict) else {}


def settings(config):
    j = dict((config or {}).get('jira') or {})
    j.update(saved_settings())
    j.setdefault('enabled', False)
    j.setdefault('dry_run', True)
    j.setdefault('flavor', 'server')            # server / data center (name) or cloud (accountId)
    j.setdefault('key_pattern', DEFAULT_KEY)
    j.setdefault('transition_name', 'In Test')
    j.setdefault('assign_to_reporter', True)
    j.setdefault('first_run_commits', 0)        # 0 = first run only records a baseline
    j.setdefault('max_tickets_per_run', 40)
    j.setdefault('transition_fields', '')       # 流转时必填字段的值，每行：问题类型 | 字段名 | 值
    j.setdefault('use_pr_lookup', True)         # PR 为主要来源；查询失败自动退回提交信息
    j.setdefault('timeout_seconds', 30)
    j.setdefault('mail_enabled', False)         # 构建成功后通过 Jira 给团队发邮件（jira_mail.py）
    j.setdefault('mail_issue', '')              # 用来发邮件的固定公告单
    j.setdefault('mail_users', '')              # 收件人 Jira 用户名，逗号/空格/换行分隔
    j.setdefault('mail_failure_once', True)     # 同一原因连续失败只发一次，恢复成功时再发
    j.setdefault('comment_mentions', '')        # 评论里 @ 的 Jira 用户名，逗号/空格/换行分隔
    j.setdefault('mail_groups', '')             # 也按 Jira 用户组发邮件，逗号/换行分隔（组名可含空格）
    return j


def _read_ledger():
    try:
        return json.loads(LEDGER.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _write_ledger(data):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, LEDGER)


def commits_between(repo, old, new, limit=500):
    """Commits reachable from new but not old: [{sha, subject, body}]. old=None -> []."""
    if not old:
        return []
    out = subprocess.run(['git', '-C', str(repo), 'log', f'-n{int(limit)}', '--no-color',
                          '--format=%H%x1f%an%x1f%s%x1f%b%x1e', f'{old}..{new}'],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError('git log 失败（基线提交可能已不在本仓库）: ' + out.stderr.strip()[:200])
    rows = []
    for record in out.stdout.split('\x1e'):
        record = record.strip('\n')
        if not record.strip():
            continue
        sha, author, subject, body = (record.split('\x1f') + ['', '', ''])[:4]
        rows.append({'sha': sha.strip(), 'author': author.strip(), 'subject': subject.strip(), 'body': body.strip()})
    return rows


def recent_commits(repo, new, count):
    out = subprocess.run(['git', '-C', str(repo), 'log', f'-n{int(count)}', '--no-color',
                          '--format=%H%x1f%an%x1f%s%x1f%b%x1e', new], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return []
    rows = []
    for record in out.stdout.split('\x1e'):
        if record.strip():
            sha, author, subject, body = (record.strip('\n').split('\x1f') + ['', '', ''])[:4]
            rows.append({'sha': sha.strip(), 'author': author.strip(), 'subject': subject.strip(), 'body': body.strip()})
    return rows


def _split_message(sha, message, author=''):
    subject, _, body = (message or '').partition('\n')
    return {'sha': sha, 'author': author, 'subject': subject.strip(), 'body': body.strip()}


def _from_api(c):
    commit = c.get('commit') or {}
    who = (c.get('author') or {}).get('login') or (commit.get('author') or {}).get('name') or ''
    return _split_message(c.get('sha', ''), commit.get('message', ''), who)


class GitHubCommits:
    """Commit lists from the GitHub API, used when this task has no local Plugin clone
    (for example the Plugin build was reused from an earlier task, or the folder was moved)."""

    def __init__(self, getter):
        self._get = getter

    def between(self, old, new, limit=500):
        if not old:
            return []
        data = self._get(f'/compare/{quote(old)}...{quote(new)}')
        commits = data.get('commits') or []
        if data.get('status') not in ('ahead', 'identical'):
            raise RuntimeError('GitHub 比较结果不是单向前进（基线可能已不在来源分支）')
        rows = [_from_api(c) for c in commits]
        return list(reversed(rows))[:limit]                    # newest first, like git log

    def recent(self, new, count):
        data = self._get(f'/commits?sha={quote(new)}&per_page={int(count)}')
        return [_from_api(c) for c in data or []]


def github_api_base(plugin_url):
    """https://host/org/repo -> (https://host/api/v3, 'org/repo'); github.com uses api.github.com."""
    parts = urlsplit(plugin_url or '')
    path = parts.path.strip('/').removesuffix('.git')
    if parts.scheme != 'https' or path.count('/') != 1:
        raise ValueError('无法从 plugin_url 推出 GitHub 仓库')
    host = 'https://api.github.com' if parts.netloc == 'github.com' else f'https://{parts.netloc}/api/v3'
    return host, path


class PRLookup:
    """Find the pull request(s) a commit came in with. Any API failure disables the lookup
    for the rest of the run (callers then fall back to commit text) and is reported."""

    def __init__(self, plugin_url, token, timeout=30, ca_bundle=None, max_calls=200):
        self.base, self.repo = github_api_base(plugin_url)
        self.token, self.timeout, self.ca_bundle = token, timeout, ca_bundle
        self.max_calls, self.calls, self.cache, self.error = max_calls, 0, {}, None

    def repo_get(self, path):
        """GET /repos/<repo><path>; errors are raised (used for commit lists)."""
        try:
            return self._get(f'/repos/{self.repo}{path}')
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'GitHub 读取提交失败 HTTP {exc.code}') from None

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json'})
        ctx = None
        if self.ca_bundle:
            import ssl
            ctx = ssl.create_default_context(cafile=self.ca_bundle)
        with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
            return json.loads(r.read() or b'[]')

    def for_commit(self, sha):
        if self.error or self.calls >= self.max_calls:
            return None
        self.calls += 1
        try:
            prs = self._get(f'/repos/{self.repo}/commits/{sha}/pulls')
        except urllib.error.HTTPError as exc:
            self.error = f'GitHub PR 查询失败 HTTP {exc.code}，改用提交信息'
            return None
        except Exception as exc:
            self.error = f'GitHub PR 查询失败 {type(exc).__name__}，改用提交信息'
            return None
        if not prs:
            return None
        pr = next((p for p in prs if p.get('merged_at')), prs[0])
        info = {'number': pr.get('number'), 'title': pr.get('title') or '', 'body': pr.get('body') or '',
                'head': (pr.get('head') or {}).get('ref', ''), 'url': pr.get('html_url') or '',
                'author': (pr.get('user') or {}).get('login') or ''}
        self.cache[sha] = info
        return info

    def display_name(self, login):
        """GitHub profile name for a login (staff numbers are logins here), else the login.
        Best effort: any failure just keeps the login."""
        if not login or self.calls >= self.max_calls:
            return login
        names = self.__dict__.setdefault('names', {})
        if login not in names:
            self.calls += 1
            try:
                names[login] = str((self._get(f'/users/{quote(login)}') or {}).get('name') or '').strip()[:80]
            except Exception:
                names[login] = ''
        return names[login] or login


def tickets_from_commits(commits, pattern, llm=None, prs=None):
    """Return ({key: info}, []). Every ticket key mentioned anywhere - PR title, PR branch,
    PR description, commit subject or commit body - is included. info keeps the first
    commit's fields (sha, subject, body, author, source) and `refs`: every PR / commit that
    mentioned the key, so the Jira comment can say why the ticket was picked up and by whom.
    `llm` is kept for compatibility and no longer used here."""
    regex = re.compile(pattern)
    found = {}
    for c in commits:
        pr = prs.for_commit(c['sha']) if prs else None
        places = []
        if pr:
            places += [('PR标题/分支', pr['title'] + '\n' + pr['head']), ('PR描述', pr['body'])]
        places += [('提交标题', c['subject']), ('提交正文', c['body'])]
        if pr and prs is not None and hasattr(prs, 'display_name') and pr.get('author'):
            pr = {**pr, 'author': prs.display_name(pr['author'])}
        ref = {'sha': c['sha'], 'subject': c['subject'], 'author': c.get('author', ''),
               'pr': ({k: pr.get(k) for k in ('number', 'title', 'url', 'author')} if pr else None)}
        for name, text in places:
            for key in dict.fromkeys(regex.findall(text or '')):
                info = found.setdefault(key, {**c, 'subject': (pr['title'] if pr and pr['title'] else c['subject']),
                                              'pr_body': (pr.get('body') or '') if pr else '',
                                              'source': name, 'refs': []})
                if all(r['sha'] != ref['sha'] for r in info['refs']):
                    info['refs'].append(ref)
    return found, []


def parse_field_rules(text):
    """Lines of '问题类型 | 字段名 | 值' (type may be * or omitted) -> [(type, field, value)]."""
    rules = []
    for number, raw in enumerate((text or '').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) == 2:
            parts = ['*'] + parts
        if len(parts) != 3 or not all(parts):
            raise ValueError(f'第 {number} 行格式不对，应为：问题类型 | 字段名 | 值（问题类型可写 * 表示全部）')
        rules.append((parts[0].lower(), parts[1].lower(), parts[2]))
    return rules


def _field_value(meta, value):
    allowed = meta.get('allowedValues') or []
    if allowed:
        for option in allowed:
            names = [str(option.get(k, '')).strip().lower() for k in ('value', 'name', 'id')]
            if value.strip().lower() in names:
                for k in ('value', 'name', 'id'):
                    if option.get(k) is not None:
                        picked = {k: option[k]}
                        break
                return [picked] if (meta.get('schema') or {}).get('type') == 'array' else picked
        shown = '、'.join(str(o.get('value') or o.get('name') or o.get('id')) for o in allowed)[:200]
        raise ValueError(f'“{value}” 不在可选值里：{shown}')
    return [value] if (meta.get('schema') or {}).get('type') == 'array' else value


def plan_fields(required, issue_type, rules):
    """required: Jira's transition 'fields' map. Returns (payload, missing_names, bad_messages, auto_picks).
    A required choice field without a rule gets Jira's first allowed value (reported in auto_picks)."""
    payload, missing, bad, auto = {}, [], [], []
    kind = (issue_type or '').strip().lower()
    for fid, meta in (required or {}).items():
        if not meta.get('required') or meta.get('hasDefaultValue'):
            continue
        name = meta.get('name') or fid
        hits = [r for r in rules if r[1] in (name.lower(), fid.lower()) and r[0] in (kind, '*')]
        hits.sort(key=lambda r: r[0] == '*')            # a rule for this issue type wins over '*'
        if not hits:
            allowed = meta.get('allowedValues') or []
            if allowed:                                  # no rule: take the first option Jira offers
                first = allowed[0]
                key = next(k for k in ('value', 'name', 'id') if first.get(k) is not None)
                payload[fid] = [{key: first[key]}] if (meta.get('schema') or {}).get('type') == 'array' else {key: first[key]}
                auto.append(f'{name}={first[key]}')
            else:
                missing.append(name)
            continue
        try:
            payload[fid] = _field_value(meta, hits[0][2])
        except ValueError as exc:
            bad.append(f'{name}: {exc}')
    return payload, missing, bad, auto


class NoSuchTransition(RuntimeError):
    """The ticket's current status has no transition with the configured name."""


class JiraNotFound(RuntimeError):
    """HTTP 404 from Jira (no such issue, or not visible to this token)."""


class UnknownTicket(Exception):
    """The key looked like a ticket but Jira has no such issue; it is ignored, not an error."""


def _jira_reason(exc):
    """Jira's own explanation from an error response ({errorMessages, errors}), shortened."""
    try:
        data = json.loads(exc.read(20000) or b'{}')
    except Exception:
        return ''
    if not isinstance(data, dict):
        return ''
    parts = [str(m) for m in data.get('errorMessages') or []]
    errors = data.get('errors')
    if isinstance(errors, dict):
        parts += [f'{k}: {v}' for k, v in errors.items()]
    text = safe_text('；'.join(parts))[:300]
    return f'（Jira 说明：{text}）' if text else ''


class JiraClient:
    def __init__(self, base_url, *, flavor='server', timeout=30, ca_bundle=None, env=None):
        parts = urlsplit(base_url)
        if parts.scheme != 'https' or not parts.netloc:
            raise ValueError('jira.base_url 必须是 https 地址')
        self.base = base_url.rstrip('/')
        self.flavor = flavor
        self.timeout = timeout
        self.ca_bundle = ca_bundle
        use_keychain = env is None
        env = env if env is not None else os.environ
        token = env.get('JIRA_TOKEN', '').strip()
        if not token and use_keychain and not (env.get('JIRA_USER') and env.get('JIRA_API_TOKEN')):
            from .auth import read_jira_token
            token = (read_jira_token() or '').strip()
        user, api = env.get('JIRA_USER', '').strip(), env.get('JIRA_API_TOKEN', '').strip()
        if token:
            self.auth = 'Bearer ' + token
        elif user and api:
            self.auth = 'Basic ' + base64.b64encode(f'{user}:{api}'.encode()).decode()
        else:
            raise ValueError('未设置 Jira Token：请在页面 Jira 面板保存，或设置 JIRA_TOKEN 环境变量')

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            'Authorization': self.auth, 'Accept': 'application/json', 'Content-Type': 'application/json'})
        ctx = None
        if self.ca_bundle:
            import ssl
            ctx = ssl.create_default_context(cafile=self.ca_bundle)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            message = f'Jira {method} {path.split("?")[0]} -> HTTP {exc.code}' + _jira_reason(exc)
            raise (JiraNotFound(message) if exc.code == 404 else RuntimeError(message)) from None
        return json.loads(raw) if raw else {}

    def myself(self):
        return self._call('GET', '/rest/api/2/myself')

    def transitions(self, key, expand=False):
        query = '?expand=transitions.fields' if expand else ''
        return self._call('GET', f'/rest/api/2/issue/{quote(key)}/transitions{query}').get('transitions', [])

    def issue(self, key):
        return self._call('GET', f'/rest/api/2/issue/{quote(key)}?fields=reporter,status,summary,issuetype')

    def comments(self, key):
        return self._call('GET', f'/rest/api/2/issue/{quote(key)}/comment').get('comments', [])

    def add_comment(self, key, text):
        return self._call('POST', f'/rest/api/2/issue/{quote(key)}/comment', {'body': text})

    def assign(self, key, reporter):
        if self.flavor == 'cloud':
            return self._call('PUT', f'/rest/api/2/issue/{quote(key)}/assignee', {'accountId': reporter.get('accountId')})
        return self._call('PUT', f'/rest/api/2/issue/{quote(key)}/assignee', {'name': reporter.get('name')})

    def plan_transition(self, key, name, issue_type='', rules=()):
        """Read-only: find the transition and work out the required fields it needs."""
        options = self.transitions(key, expand=True)
        wanted = [t for t in options if t.get('name', '').strip().lower() == name.strip().lower()]
        if not wanted:
            error = NoSuchTransition('当前状态下没有名为 ' + name + ' 的流转: ' + ', '.join(t.get('name', '?') for t in options))
            error.available = [t.get('name', '?') for t in options]
            raise error
        payload, missing, bad, auto = plan_fields(wanted[0].get('fields'), issue_type, rules)
        return {'id': wanted[0]['id'], 'fields': payload, 'missing': missing, 'bad': bad, 'auto': auto}

    def do_transition(self, key, plan):
        body = {'transition': {'id': plan['id']}}
        if plan['fields']:
            body['fields'] = plan['fields']
        return self._call('POST', f'/rest/api/2/issue/{quote(key)}/transitions', body)


def repo_web_url(plugin_url):
    """https://host/org/repo(.git) -> https://host/org/repo, or '' when it is not like that."""
    parts = urlsplit(plugin_url or '')
    path = parts.path.strip('/').removesuffix('.git')
    if parts.scheme != 'https' or not parts.netloc or path.count('/') != 1:
        return ''
    return f'https://{parts.netloc}/{path}'


def _without_key(title, key):
    """'TRANSACT-1: Fix x' -> 'Fix x' when TRANSACT-1 is this ticket (it is already on the page)."""
    if not key:
        return title
    return re.sub(r'^\s*[\[(]?' + re.escape(key) + r'[\])]?\s*[:\-–|]?\s*', '', title or '').strip() or title


def ref_items(info, key=''):
    """Each PR / commit that pulled the ticket in: {kind, label, title, url, author}."""
    repo = info.get('repo_url', '')
    refs = info.get('refs') or [{'sha': info.get('sha', ''), 'subject': info.get('subject', ''),
                                 'author': info.get('author', ''), 'pr': None}]
    items, seen_prs = [], set()
    for ref in refs:
        pr = ref.get('pr')
        if pr and pr.get('number') is not None:
            if pr['number'] in seen_prs:
                continue
            seen_prs.add(pr['number'])
            items.append({'kind': 'PR', 'label': f'PR #{pr["number"]}',
                          'title': safe_text(_without_key(pr.get('title') or '', key))[:200],
                          'url': pr.get('url') or (f'{repo}/pull/{pr["number"]}' if repo else ''),
                          'author': safe_text(pr.get('author') or '')[:80]})
        else:
            sha = ref.get('sha') or ''
            items.append({'kind': 'Commit', 'label': f'Commit {sha[:10]}',
                          'title': safe_text(_without_key(ref.get('subject') or '', key))[:200],
                          'url': f'{repo}/commit/{sha}' if repo and sha else '',
                          'author': safe_text(ref.get('author') or '')[:80]})
    return items


def describe_refs(info, key=''):
    """Plain English lines (kept for the delivery page and the plain-text email)."""
    return [f'- {i["label"]} "{i["title"]}"' + (f' by {i["author"]}' if i['author'] else '')
            + (f' {i["url"]}' if i['url'] else '') for i in ref_items(info, key)]


WIKI_SPECIAL = re.compile(r'([\[\]{}|*_^~+!#])')


def wiki_text(text):
    """Plain text that Jira wiki markup shows as typed (no accidental bold, links or macros)."""
    return WIKI_SPECIAL.sub(r'\\\1', (text or '').replace('\n', ' '))


def comment_text(marker, link, gmx_build, branch, items, manual, mentions, note=None):
    """The Jira comment (wiki markup). One highlighted box with the state and the install
    link, then why the ticket is here, then anything a person must do. The marker goes
    last in grey: it is how a re-run knows this build already commented."""
    head = '*Ready to test*' + (f' · GMX #{gmx_build}' if gmx_build else '') + (f' · {wiki_text(branch)}' if branch else '')
    lines = ['{panel:bgColor=#E9F5EE|borderColor=#B7DCC4}', head,
             f'*[Install from TestFairy|{link}]*', '{panel}']
    if note:
        lines.append('*For testers:* ' + wiki_text(note))
    if items:
        lines.append('*Included because of*')
        for i in items:
            label = f'[{i["label"]}|{i["url"]}]' if i['url'] else i['label']
            lines.append(f'* {label} {wiki_text(i["title"])}' + (f' · {wiki_text(i["author"])}' if i['author'] else ''))
    if manual:
        lines += ['', '{color:#9A5B00}*Status not changed:* ' + wiki_text(manual) + '{color}']
    if mentions:
        lines += ['', mentions]
    lines.append('{color:#97A0AF}' + marker + '{color}')
    return '\n'.join(lines)


def _manual_reason(exc_or_plan, status, name):
    """Plain English for the Jira comment (Jira readers do not see this tool's Chinese UI)."""
    please = f"Please move it to '{name}' manually."
    if isinstance(exc_or_plan, NoSuchTransition):
        return f"'{status}' cannot move to '{name}' directly. {please}"
    if isinstance(exc_or_plan, dict):
        parts = []
        if exc_or_plan.get('missing'):
            parts.append('required field(s) ' + ', '.join(exc_or_plan['missing']) + ' have no configured value')
        if exc_or_plan.get('bad'):
            parts.append('configured value not allowed for ' + ', '.join(b.split(':', 1)[0] for b in exc_or_plan['bad']))
        text = '; '.join(parts)
        return text[:1].upper() + text[1:] + '. ' + please
    return f"The '{name}' move could not be checked ({type(exc_or_plan).__name__}). {please}"


def mention_line(text, flavor='server'):
    """'cc [~a] [~b]' for the configured Jira users (Server/DC wiki markup; Cloud uses
    [~accountid:...]). Mentioned users get Jira's own notification."""
    from .jira_mail import parse_users
    try:
        users = parse_users(text or '')
    except ValueError:
        return ''
    if not users:
        return ''
    return 'cc ' + ' '.join(f'[~accountid:{u}]' if flavor == 'cloud' else f'[~{u}]' for u in users)


def process_ticket(client, key, info, link, build_label, conf, llm, dry_run, gmx_build=None, branch=''):
    # The marker identifies THIS build task, so a ticket gets one comment per build it is in.
    # (The GMX build number alone is not unique: every new upload branch starts at #2.)
    marker = f'[app-build:{build_label}]'
    name = conf['transition_name']
    result = {'key': key, 'steps': []}
    try:
        issue = client.issue(key)
    except JiraNotFound:
        raise UnknownTicket(key) from None
    fields = issue.get('fields', {})
    result['summary'] = safe_text(str(fields.get('summary') or ''))[:200]
    reporter = fields.get('reporter') or {}
    status = (fields.get('status') or {}).get('name', '')
    issue_type = (fields.get('issuetype') or {}).get('name', '')
    items = ref_items(info, key)
    result['ref_items'] = items
    result['refs'] = describe_refs(info, key)
    need_move = status.strip().lower() != name.strip().lower()
    plan = None
    if need_move:
        # Whatever the status, the ticket still gets the comment and the assignment; only the
        # status change is left to a person when it cannot be done automatically.
        try:
            plan = client.plan_transition(key, name, issue_type, parse_field_rules(conf.get('transition_fields')))
            if plan['missing'] or plan['bad']:
                result['manual'] = _manual_reason(plan, status, name)
        except UnknownTicket:
            raise
        except Exception as exc:
            result['manual'] = _manual_reason(exc, status, name)
            if isinstance(exc, NoSuchTransition):
                result['available'] = getattr(exc, 'available', [])
    result['status'] = status
    note = None
    if conf.get('ai_tester_note') and llm:
        from .local_llm import tester_note
        try:
            note = tester_note(llm, _without_key(info.get('subject', ''), key), info.get('pr_body') or info.get('body', ''))
        except Exception:
            note = None
    if note:
        result['tester_note'] = note
    mentions = mention_line(conf.get('comment_mentions', ''), conf.get('flavor', 'server'))
    text = comment_text(marker, link, gmx_build, branch, items, result.get('manual'), mentions, note)
    if dry_run:
        step = (f'transition -> {name} (当前 {status}, 类型 {issue_type or "?"})' if need_move
                else f'已在 {name}，不需要流转')
        if result.get('manual'):
            step += f'；需手动：{result["manual"]}'
        elif plan and plan.get('auto'):
            step += '；自动选第一个可选值：' + '、'.join(plan['auto'])
        elif plan and plan['fields']:
            step += '；将按配置填写必填字段'
        result['steps'] = [f'[dry-run] comment: Ready to test, {len(items)} 个来源' + ('，含状态提醒' if result.get('manual') else ''), f'[dry-run] assign -> {reporter.get("displayName") or reporter.get("name")}', f'[dry-run] {step}']
        return result
    if not any(marker in (c.get('body') or '') for c in client.comments(key)):
        client.add_comment(key, text)
        result['steps'].append('comment')
    if conf['assign_to_reporter'] and (reporter.get('name') or reporter.get('accountId')):
        client.assign(key, reporter)
        result['steps'].append('assign')
        result['assignee'] = safe_text(str(reporter.get('displayName') or reporter.get('name') or ''))[:80]
    if need_move and not result.get('manual'):
        try:
            client.do_transition(key, plan)
            result['status'] = name
            result['steps'].append('transition' + (' (自动选: ' + '、'.join(plan['auto']) + ')' if plan.get('auto') else ''))
        except Exception as exc:
            result['manual'] = f"Moving to '{name}' failed ({safe_text(str(exc))[:120]}). Please move it manually."
    return result


def notify_jira(task_id, client=None, llm=None, config=None, preview=None):
    """Serialise formal runs: two builds finishing together must not read the same ledger."""
    if preview is not None:
        return _notify_jira(task_id, client, llm, config, preview)
    import fcntl
    lock_path = LEDGER.with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _notify_jira(task_id, client, llm, config, preview)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _notify_jira(task_id, client=None, llm=None, config=None, preview=None):
    """preview=N: web-page rehearsal on a finished task. Looks at its last N commits, never
    writes to Jira, the ledger or the task state, and returns the report."""
    row = task(task_id)
    if row['status'] != 'SUCCESS':
        if preview is not None:
            raise ValueError('只能对状态为 SUCCESS 的构建做演练')
        return
    config = config or load_config()
    conf = settings(config)
    if preview is not None:
        conf = {**conf, 'dry_run': True}
    elif not conf['enabled']:
        return
    params = json.loads(row['params'])
    state = read_state(task_id)
    runtime = state.get('runtime', {})
    source_sha = runtime.get('plugin_source_sha')
    link = (runtime.get('testfairy_app') or {}).get('share_url')
    if not (isinstance(link, str) and link.startswith('https://')):
        link = None  # never fall back to the Jenkins job URL: testers need the real App link
    build = runtime.get('gmx_build_number', runtime.get('gmx_formal_number', task_id))
    if not source_sha or not link:
        report = {'status': 'SKIPPED', 'reason': '缺少来源提交，或本次构建没有 App 链接（New build 里需启用 TestFairy 才会取链）'}
        if preview is not None:
            return report
        patch_runtime(task_id, jira_report=report)
        return
    if preview is None and (runtime.get('jira_report') or {}).get('status') in ('DONE', 'DRY_RUN'):
        return
    repo = task_folder(task_id) / 'plugin'
    local = (repo / '.git').is_dir()
    branch = params.get('plugin_source') or 'unknown'
    ledger = _read_ledger()
    old = (ledger.get(branch) or {}).get('last_sha')
    llm = llm if llm is not None else get_llm(config)
    from .ai_setup import feature_on
    conf = {**conf, 'ai_tester_note': bool(llm) and (feature_on(config, 'tester_note') or preview is not None)}
    github = None

    def remote():
        nonlocal github
        if github is None:
            from .config import token_or_error
            github = PRLookup(params.get('plugin_url') or config['github']['plugin_url'], token_or_error(config),
                              timeout=conf['timeout_seconds'], ca_bundle=conf.get('ca_bundle'))
        return GitHubCommits(github.repo_get)

    def between(a, b):
        if local:
            try:
                return commits_between(repo, a, b)
            except RuntimeError:
                pass                                   # baseline not in this clone: ask GitHub
        return remote().between(a, b)

    def recent(b, n):
        rows = recent_commits(repo, b, n) if local else []
        return rows or remote().recent(b, n)

    if not local:
        action('JIRA', '本任务没有本地 Plugin 代码（可能复用了之前的 Plugin 构建），改从 GitHub 读取提交')
    if preview is not None:
        old = None
        commits = recent(source_sha, int(preview))
    elif old is None:
        commits = recent(source_sha, conf['first_run_commits']) if conf['first_run_commits'] else []
        if not commits:
            ledger[branch] = {'last_sha': source_sha}
            _write_ledger(ledger)
            patch_runtime(task_id, jira_report={'status': 'BASELINE', 'reason': '首次运行，仅记录基线，不改任何 ticket'})
            action('JIRA', '首次运行：记录基线提交，不改 ticket')
            return
    elif old == source_sha:
        commits = []                                   # nothing new since the last processed build
    else:
        commits = between(old, source_sha)
    prs = None
    if conf['use_pr_lookup'] and commits:
        try:
            if github is not None:
                prs = github
            else:
                from .config import token_or_error
                prs = PRLookup(params.get('plugin_url') or config['github']['plugin_url'], token_or_error(config),
                               timeout=conf['timeout_seconds'], ca_bundle=conf.get('ca_bundle'))
        except Exception as exc:
            action('JIRA', safe_text(f'PR 查询不可用，改用提交信息: {exc}'))
    found, ambiguous = tickets_from_commits(commits, conf['key_pattern'], llm, prs)
    repo_url = repo_web_url(params.get('plugin_url') or (config.get('github') or {}).get('plugin_url', ''))
    for info in found.values():
        info['repo_url'] = repo_url
    keys = list(found)                       # every mentioned ticket; only unknown keys are skipped
    if len(keys) > conf['max_tickets_per_run']:
        action('JIRA', f'本次提到 {len(keys)} 张 ticket，超过提醒值 {conf["max_tickets_per_run"]}；仍全部处理')
    dry = bool(conf['dry_run'])
    results, errors, ignored = [], [], []
    if keys:
        client = client or JiraClient(conf.get('base_url', ''), flavor=conf['flavor'],
                                      timeout=conf['timeout_seconds'], ca_bundle=conf.get('ca_bundle'))
    for key in keys:
        try:
            results.append(process_ticket(client, key, found[key], link, task_id, conf, llm, dry, gmx_build=build,
                                          branch=params.get('plugin_branch', '')))
        except UnknownTicket:
            ignored.append(key)
            action('JIRA', f'{key} 在 Jira 中查不到，已忽略')
        except Exception as exc:
            errors.append({'key': key, 'error': safe_text(str(exc))[:200]})
            action('JIRA', safe_text(f'{key} 处理失败: {exc}'))
    report = {'status': 'PREVIEW' if preview is not None else ('DRY_RUN' if dry else ('DONE' if not errors else 'PARTIAL')),
              'at': datetime.now().isoformat(timespec='seconds'), 'link': link, 'build': build,
              'transition': conf['transition_name'],
              'commits_scanned': len(commits), 'tickets': results,
              'errors': errors, 'ignored_keys': ignored, 'ambiguous_commits': ambiguous, 'from': old, 'to': source_sha,
              'needs_manual': [{'key': r['key'], 'reason': r['manual'] + (' (available: ' + ', '.join(r['available']) + ')' if r.get('available') else '')}
                             for r in results if r.get('manual')],
              'ticket_sources': {k: found[k].get('source') for k in keys},
              'pr_lookup': ('未启用' if not prs else (prs.error or f'正常，查询 {prs.calls} 次'))}
    if preview is not None:
        return report
    patch_runtime(task_id, jira_report=report)
    if not dry and not errors:
        ledger[branch] = {'last_sha': source_sha}
        _write_ledger(ledger)
    action('JIRA', f'{report["status"]}: {len(results)} 个 ticket, {len(errors)} 个失败, {len(ambiguous)} 个提交无法确定归属')
