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
import urllib.error
import urllib.request
from urllib.parse import urlsplit, quote
from .config import DATA, load_config
from .store import task, read_state, patch_runtime, task_folder
from .telemetry import action, safe_text
from .local_llm import get_llm, pick_primary_ticket, tester_summary

DEFAULT_KEY = r'\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b'
LEDGER = DATA / 'jira_ledger.json'


def settings(config):
    j = dict((config or {}).get('jira') or {})
    j.setdefault('enabled', False)
    j.setdefault('dry_run', True)
    j.setdefault('flavor', 'server')            # server / data center (name) or cloud (accountId)
    j.setdefault('key_pattern', DEFAULT_KEY)
    j.setdefault('transition_name', 'In Test')
    j.setdefault('assign_to_reporter', True)
    j.setdefault('first_run_commits', 0)        # 0 = first run only records a baseline
    j.setdefault('max_tickets_per_run', 40)
    j.setdefault('timeout_seconds', 30)
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
                          '--format=%H%x1f%s%x1f%b%x1e', f'{old}..{new}'],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError('git log 失败（基线提交可能已不在本仓库）: ' + out.stderr.strip()[:200])
    rows = []
    for record in out.stdout.split('\x1e'):
        record = record.strip('\n')
        if not record.strip():
            continue
        sha, subject, body = (record.split('\x1f') + ['', ''])[:3]
        rows.append({'sha': sha.strip(), 'subject': subject.strip(), 'body': body.strip()})
    return rows


def recent_commits(repo, new, count):
    out = subprocess.run(['git', '-C', str(repo), 'log', f'-n{int(count)}', '--no-color',
                          '--format=%H%x1f%s%x1f%b%x1e', new], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return []
    rows = []
    for record in out.stdout.split('\x1e'):
        if record.strip():
            sha, subject, body = (record.strip('\n').split('\x1f') + ['', ''])[:3]
            rows.append({'sha': sha.strip(), 'subject': subject.strip(), 'body': body.strip()})
    return rows


def tickets_from_commits(commits, pattern, llm=None):
    """Return ({key: {'subject','body','sha'}}, [ambiguous commit subjects]).

    Keys in the subject win over keys in the body. Several different keys: the offline
    model may pick one (validated against the candidates); without it the commit is skipped
    and reported instead of guessing."""
    regex = re.compile(pattern)
    found, ambiguous = {}, []
    for c in commits:
        keys = list(dict.fromkeys(regex.findall(c['subject'])))
        if not keys:
            keys = list(dict.fromkeys(regex.findall(c['body'])))
        if not keys:
            continue
        if len(keys) > 1:
            chosen = pick_primary_ticket(llm, keys, c['subject'] + '\n' + c['body'])
            if not chosen:
                ambiguous.append(c['subject'][:120])
                continue
            keys = [chosen]
        found.setdefault(keys[0], c)
    return found, ambiguous


class JiraClient:
    def __init__(self, base_url, *, flavor='server', timeout=30, ca_bundle=None, env=None):
        parts = urlsplit(base_url)
        if parts.scheme != 'https' or not parts.netloc:
            raise ValueError('jira.base_url 必须是 https 地址')
        self.base = base_url.rstrip('/')
        self.flavor = flavor
        self.timeout = timeout
        self.ca_bundle = ca_bundle
        env = env if env is not None else os.environ
        token = env.get('JIRA_TOKEN', '').strip()
        user, api = env.get('JIRA_USER', '').strip(), env.get('JIRA_API_TOKEN', '').strip()
        if token:
            self.auth = 'Bearer ' + token
        elif user and api:
            self.auth = 'Basic ' + base64.b64encode(f'{user}:{api}'.encode()).decode()
        else:
            raise ValueError('未设置 JIRA_TOKEN（或 JIRA_USER + JIRA_API_TOKEN）环境变量')

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
            raise RuntimeError(f'Jira {method} {path.split("?")[0]} -> HTTP {exc.code}') from None
        return json.loads(raw) if raw else {}

    def issue(self, key):
        return self._call('GET', f'/rest/api/2/issue/{quote(key)}?fields=reporter,status,summary')

    def comments(self, key):
        return self._call('GET', f'/rest/api/2/issue/{quote(key)}/comment').get('comments', [])

    def add_comment(self, key, text):
        return self._call('POST', f'/rest/api/2/issue/{quote(key)}/comment', {'body': text})

    def assign(self, key, reporter):
        if self.flavor == 'cloud':
            return self._call('PUT', f'/rest/api/2/issue/{quote(key)}/assignee', {'accountId': reporter.get('accountId')})
        return self._call('PUT', f'/rest/api/2/issue/{quote(key)}/assignee', {'name': reporter.get('name')})

    def transition(self, key, name):
        options = self._call('GET', f'/rest/api/2/issue/{quote(key)}/transitions').get('transitions', [])
        wanted = [t for t in options if t.get('name', '').strip().lower() == name.strip().lower()]
        if not wanted:
            raise RuntimeError('当前状态下没有名为 ' + name + ' 的流转: ' + ', '.join(t.get('name', '?') for t in options))
        return self._call('POST', f'/rest/api/2/issue/{quote(key)}/transitions', {'transition': {'id': wanted[0]['id']}})


def process_ticket(client, key, info, link, build_label, conf, llm, dry_run):
    marker = f'[app-build:{build_label}]'
    result = {'key': key, 'steps': []}
    issue = client.issue(key)
    fields = issue.get('fields', {})
    reporter = fields.get('reporter') or {}
    status = (fields.get('status') or {}).get('name', '')
    summary = tester_summary(llm, info['subject'], info.get('body', ''))
    text = f'{marker} 新测试包已构建：{link}\n改动：{summary}'
    if dry_run:
        result['steps'] = [f'[dry-run] comment: {text[:80]}', f'[dry-run] assign -> {reporter.get("displayName") or reporter.get("name")}',
                           f'[dry-run] transition -> {conf["transition_name"]} (当前 {status})']
        return result
    if not any(marker in (c.get('body') or '') for c in client.comments(key)):
        client.add_comment(key, text)
        result['steps'].append('comment')
    if conf['assign_to_reporter'] and (reporter.get('name') or reporter.get('accountId')):
        client.assign(key, reporter)
        result['steps'].append('assign')
    if status.strip().lower() != conf['transition_name'].strip().lower():
        client.transition(key, conf['transition_name'])
        result['steps'].append('transition')
    return result


def notify_jira(task_id, client=None, llm=None, config=None):
    row = task(task_id)
    if row['status'] != 'SUCCESS':
        return
    config = config or load_config()
    conf = settings(config)
    if not conf['enabled']:
        return
    params = json.loads(row['params'])
    state = read_state(task_id)
    runtime = state.get('runtime', {})
    source_sha = runtime.get('plugin_source_sha')
    link = (runtime.get('testfairy_app') or {}).get('share_url') or params.get('gmx_job')
    build = runtime.get('gmx_build_number', runtime.get('gmx_formal_number', task_id))
    if not source_sha or not link:
        patch_runtime(task_id, jira_report={'status': 'SKIPPED', 'reason': '缺少来源提交或 App 链接'})
        return
    if (runtime.get('jira_report') or {}).get('status') in ('DONE', 'DRY_RUN'):
        return
    repo = task_folder(task_id) / 'plugin'
    branch = params.get('plugin_source') or 'unknown'
    ledger = _read_ledger()
    old = (ledger.get(branch) or {}).get('last_sha')
    llm = llm if llm is not None else get_llm(config)
    if old is None:
        commits = recent_commits(repo, source_sha, conf['first_run_commits']) if conf['first_run_commits'] else []
        if not commits:
            ledger[branch] = {'last_sha': source_sha}
            _write_ledger(ledger)
            patch_runtime(task_id, jira_report={'status': 'BASELINE', 'reason': '首次运行，仅记录基线，不改任何 ticket'})
            action('JIRA', '首次运行：记录基线提交，不改 ticket')
            return
    else:
        commits = commits_between(repo, old, source_sha)
    found, ambiguous = tickets_from_commits(commits, conf['key_pattern'], llm)
    keys = list(found)[:conf['max_tickets_per_run']]
    dry = bool(conf['dry_run'])
    results, errors = [], []
    if keys:
        client = client or JiraClient(conf.get('base_url', ''), flavor=conf['flavor'],
                                      timeout=conf['timeout_seconds'], ca_bundle=conf.get('ca_bundle'))
    for key in keys:
        try:
            results.append(process_ticket(client, key, found[key], link, build, conf, llm, dry))
        except Exception as exc:
            errors.append({'key': key, 'error': safe_text(str(exc))[:200]})
            action('JIRA', safe_text(f'{key} 处理失败: {exc}'))
    report = {'status': 'DRY_RUN' if dry else ('DONE' if not errors else 'PARTIAL'), 'tickets': results,
              'errors': errors, 'ambiguous_commits': ambiguous, 'from': old, 'to': source_sha}
    patch_runtime(task_id, jira_report=report)
    if not dry and not errors:
        ledger[branch] = {'last_sha': source_sha}
        _write_ledger(ledger)
    action('JIRA', f'{report["status"]}: {len(results)} 个 ticket, {len(errors)} 个失败, {len(ambiguous)} 个提交无法确定归属')
