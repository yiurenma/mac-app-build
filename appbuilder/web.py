"""Loopback-only Web Console for the existing CLI engine (no Jenkins/Git API server).

No external web framework, no telemetry endpoint, no credential persistence in the
browser or task DB. Browser HTTP actions are CSRF-checked and same-origin only.
"""
from __future__ import annotations

import hmac
from copy import deepcopy
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace

from .config import DATA, ROOT, load_config, token_or_error, parallel_limit
from . import __version__
from .store import task, task_list, create_task, active_count, update_task, update_task_params, task_folder, reserve_resume
from .telemetry import safe_text
from .versions import validate_args

STATIC = Path(__file__).parent / 'webui'
HOST = '127.0.0.1'
TASK_ID = re.compile(r'^\d{12}-[0-9a-f]{6}$')
IMAGE_FILE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}\.png$')
MAX_REQUEST_BYTES = 16_384
MAX_LOG_BYTES = 96_000
FINISHED = {'SUCCESS', 'FAILED', 'TIMEOUT', 'NEEDS_ATTENTION', 'INTERRUPTED'}


def _jenkins_links(row, params):
    """Safe clickable Jenkins links, including Trigger -> Callback when known.

    No request to Jenkins is made here: metadata comes from the local task
    state. Refuse embedded credentials, fragments, queries and non-HTTPS URLs.
    """
    from .store import read_state
    result = []
    try:
        runtime = read_state(row['id']).get('runtime', {})
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        runtime = {}
    for key, label, name in [('plugin_job', 'Plugin Job', 'plugin'),
                             ('gmx_job', 'GMX Job', 'gmx')]:
        raw = str(params.get(key, ''))
        parsed = urlsplit(raw)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            continue
        root = raw.rstrip('/') + '/'
        result.append({'label': label, 'url': root})
        # Jenkins link numbers are taken only from persisted build metadata.
        for runtime_key, build_label in [(name + '_formal_number', 'Build'),
                                         ('plugin_callback_number', 'Callback')] if name == 'plugin' else [(name + '_formal_number', 'Build')]:
            number = runtime.get(runtime_key)
            if isinstance(number, int) and not isinstance(number, bool) and 0 < number < 1000000:
                result.append({'label': f'{name.upper() if name == "gmx" else name.title()} {build_label} #{number}',
                               'url': root + str(number) + '/'})
    return result


def public_task(row):
    """Never send raw params/credentials to UI."""
    params = json.loads(row['params'])
    from .cli import alive, stage_indicator
    status = row['status']
    if status in ('RUNNING', 'QUEUED') and row.get('pid') and not alive(row['pid']):
        status = 'INTERRUPTED'
    return {
        'id': row['id'], 'created': row['created'], 'status': status,
        'stage': row['stage'], 'stage_display': stage_indicator(row['stage']),
        'note': safe_text(row.get('note', '')),
        'pid': row.get('pid'), 'project': params.get('project', ''),
        'country': params.get('country', ''),
        'plugin_version': params.get('plugin_version_input', ''),
        'plugin_branch': params.get('plugin_branch', ''),
        'gmx_branch': params.get('gmx_branch', ''),
        'visible': bool(params.get('visible')),
        'verbose': bool(params.get('verbose')),
        'jenkins_links': _jenkins_links(row, params),
        'delivery': delivery_result(row['id']),
    }


def delivery_result(task_id):
    from .store import read_state
    try:
        runtime=read_state(task_id).get('runtime',{})
        # Only generated, explicitly nonsecret delivery data is exposed.
        return {'app':runtime.get('testfairy_app'), 'mail_preview':runtime.get('mail_preview'), 'mail_delivery':runtime.get('mail_delivery')}
    except (OSError,ValueError):
        return {}


def get_doctor():
    """Readiness without printing secrets, without auto-running/building."""
    import contextlib
    import io
    from .cli import doctor_check
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ok = doctor_check(load_config(), strict=False)
        notes = [safe_text(line) for line in buf.getvalue().splitlines()]
    except Exception as exc:
        ok = False
        notes = ['环境检查失败：' + safe_text(str(exc))]
    return {'ready': ok, 'messages': notes}


# Safe nonsecret form defaults; never return credentials, PAT or authentication cookies.
def form_defaults(config):
    j = config.get('jenkins', {})
    b = config.get('build', {})
    return {
        'outlook_auto_send': False, 'outlook_recipients': '', 'outlook_account': '',
        'testfairy_enabled': False, 'testfairy_save_credentials': False,
        'testfairy_login_url': 'https://hsbc-mobilex.testfairy.com/login', 'testfairy_folder_path': '',
        'plugin_branch': 'develop', 'gmx_branch': 'develop',
        'plugin_version': '1.3', 'country': 'UK', 'project_name': 'PayByBank',
        'visible': True, 'verbose': True,
        'plugin_url': config['github']['plugin_url'],
        'gmx_url': config['github']['gmx_url'],
        'plugin_parent': j['plugin_parent'],
        'gmx_parent': j['gmx_parent'],
        'browser_channel': j.get('browser_channel', 'chrome'),
        'discover_poll_seconds': j.get('discover_poll_seconds', 10),
        'poll_seconds': j.get('poll_seconds', 45),
        'result_poll_seconds': j.get('result_poll_seconds', 5),
        'console_settle_seconds': j.get('console_settle_seconds', 10),
        'discover_timeout_minutes': j.get('discover_timeout_minutes', 30),
        'build_timeout_minutes': j.get('build_timeout_minutes', 180),
        'callback_timeout_minutes': j.get('callback_timeout_minutes', 30),
        'max_changed_lines': b.get('max_changed_lines', 100),
        'upload_success_pattern': b.get('upload_success_pattern', ''),
        'max_parallel': parallel_limit(config),
        'gmx_issue_key': b.get('gmx_issue_key', ''),
        'gmx_build_type': j.get('gmx_defaults', {}).get('BUILD_TYPE', 'Develop'),
        'gmx_signing_repo_branch': j.get('gmx_defaults', {}).get('SIGNING_REPO_BRANCH', 'master'),
        'gmx_sync_upload': j.get('gmx_defaults', {}).get('SYNC_UPLOAD', True),
        'gmx_skip_test': j.get('gmx_defaults', {}).get('SKIP_TEST', True),
        'gmx_force': j.get('gmx_defaults', {}).get('FORCE', True),
        'gmx_keep_alive': j.get('gmx_defaults', {}).get('KEEP_ALIVE', False),
        'gmx_custom_folder': j.get('gmx_defaults', {}).get('CUSTOM_FOLDER', ''),
        'gmx_fields': {key: j.get('gmx_fields', {}).get(key, '') for key in
                       ('ENTITIES', 'SYNC_UPLOAD', 'CUSTOM_FOLDER', 'BUILD_TYPE', 'SIGNING_REPO_BRANCH', 'SKIP_TEST', 'FORCE', 'KEEP_ALIVE')},
    }


def _plain_url(raw, label, *, maxlen=2048):
    if not isinstance(raw, str) or not raw or len(raw) > maxlen:
        raise ValueError(f'{label} 地址不能为空或过长')
    u = urlsplit(raw.strip())
    if (u.scheme != 'https' or not u.hostname or u.username or u.password
            or u.query or u.fragment or any(c.isspace() for c in raw)):
        raise ValueError(f'{label} 必须是无账号密码、查询参数的 HTTPS URL')
    return raw.strip()


def _integer_field(body, key, default, lower, upper):
    value = body.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f'{key} 无效')
    try:
        n = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{key} 必须是整数') from None
    if not lower <= n <= upper or str(value).strip() not in (str(n),):
        raise ValueError(f'{key} 必须在 {lower}～{upper} 之间')
    return n


def build_preferences(body, config, country, project):
    d = form_defaults(config)
    github = {
        'plugin_url': _plain_url(body.get('plugin_url') or d['plugin_url'], 'Plugin GitHub'),
        'gmx_url': _plain_url(body.get('gmx_url') or d['gmx_url'], 'GMX GitHub'),
    }
    jenkins = {
        'plugin_parent': _plain_url(body.get('plugin_parent') or d['plugin_parent'], 'Plugin Jenkins'),
        'gmx_parent': _plain_url(body.get('gmx_parent') or d['gmx_parent'], 'GMX Jenkins'),
    }
    browser = body.get('browser_channel') or d['browser_channel']
    if browser not in ('chrome', 'msedge', 'chromium'):
        raise ValueError('浏览器仅支持 chrome / msedge / chromium')
    jenkins['browser_channel'] = browser
    for key, lo, hi in (
        ('discover_poll_seconds', 5, 3600), ('poll_seconds', 10, 3600),
        ('result_poll_seconds', 2, 60),
        ('console_settle_seconds', 1, 120), ('discover_timeout_minutes', 1, 240),
        ('build_timeout_minutes', 1, 1440), ('callback_timeout_minutes', 1, 240)):
        jenkins[key] = _integer_field(body, key, d[key], lo, hi)
    selectors = {}
    for name in ('ENTITIES', 'SYNC_UPLOAD', 'CUSTOM_FOLDER', 'BUILD_TYPE', 'SIGNING_REPO_BRANCH', 'SKIP_TEST', 'FORCE', 'KEEP_ALIVE'):
        key = 'selector_' + name.lower()
        selector = body.get(key, d['gmx_fields'][name])
        if not isinstance(selector, str) or len(selector) > 256:
            raise ValueError(f'{key} 选择器过长或不是字符串')
        selectors[name] = selector.strip()
    jenkins['gmx_fields'] = selectors
    bt = body.get('gmx_build_type') or d['gmx_build_type']
    sr = body.get('gmx_signing_repo_branch') or d['gmx_signing_repo_branch']
    custom = body.get('gmx_custom_folder') or f'0_A_{country}_{project}'
    if not isinstance(bt, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_ .-]{0,127}', bt):
        raise ValueError('BUILD_TYPE 格式错误')
    if not isinstance(sr, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,127}', sr) or '..' in sr or '//' in sr:
        raise ValueError('SIGNING_REPO_BRANCH 分支格式错误')
    if not isinstance(custom, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', custom):
        raise ValueError('CUSTOM_FOLDER 格式错误')
    sync = body.get('gmx_sync_upload', d['gmx_sync_upload'])
    if not isinstance(sync, bool):
        raise ValueError('SYNC_UPLOAD 只能选择开启或关闭')
    checks = {}
    for field, key in [('SKIP_TEST', 'gmx_skip_test'), ('FORCE', 'gmx_force'), ('KEEP_ALIVE', 'gmx_keep_alive')]:
        value = body.get(key, d[key])
        if not isinstance(value, bool):
            raise ValueError(f'{field} 只能选择开启或关闭')
        checks[field] = value
    regex = body.get('upload_success_pattern', d['upload_success_pattern'])
    if not isinstance(regex, str) or len(regex) > 200:
        raise ValueError('上传成功日志正则过长')
    if regex:
        try:
            re.compile(regex)
        except re.error as exc:
            raise ValueError('上传成功日志正则格式错误') from exc
    build = {'max_changed_lines': _integer_field(body, 'max_changed_lines', d['max_changed_lines'], 1, 100000),
             'upload_success_pattern': regex}
    return github, jenkins, build, {
        'BUILD_TYPE': bt, 'SIGNING_REPO_BRANCH': sr,
        'SYNC_UPLOAD': sync, 'CUSTOM_FOLDER': custom, **checks,
    }


def validate_build_parameters(body, config):
    """Single validation path for immediate builds and saved schedule templates."""
    from .cli import create_params
    for key in ('visible', 'verbose'):
        if key in body and not isinstance(body[key], bool):
            raise ValueError(f'{key} 必须是开关值')
    defaults = form_defaults(config)
    country = _field(body, 'country', 30).upper()
    project = _field(body, 'project_name', 100)
    # Validate names and build version *before* creating any persistent state.
    github, jenkins, build, gmx = build_preferences(body, config, country, project)
    args = SimpleNamespace(
        plugin_branch=_field(body, 'plugin_branch', 200),
        gmx_branch=_field(body, 'gmx_branch', 200),
        gmx_issue_key=_field(body, 'gmx_issue_key', 50) if body.get('gmx_issue_key') else '',
        plugin_version=_field(body, 'plugin_version', 50),
        project_name=project, country=country,
        plugin_url=github['plugin_url'], gmx_url=github['gmx_url'],
        visible=body.get('visible', defaults['visible']),
        verbose=body.get('verbose', defaults['verbose']),
    )
    params = create_params(args, config)
    params['jenkins_settings'] = jenkins
    params['build_settings'] = build
    params['gmx_parameters'] = gmx
    from .testfairy import settings
    params['testfairy'] = settings(body, config)
    from .outlook import preferences
    params['outlook'] = preferences(body)
    return params


def create_build(body):
    """Use the same validations and persistent job engine as CLI run."""
    from .cli import doctor_check, spawn
    from .auth import store_credentials, store_github_token
    config = load_config()
    github_token = str(body.get('github_token') or '').strip()
    user = str(body.get('jenkins_user') or '').strip()
    password = str(body.get('jenkins_password') or '')
    if bool(user) != bool(password):
        raise ValueError('Jenkins 账号和密码必须同时填写，或同时留空使用 Keychain')
    if len(user) > 256 or len(password) > 4096 or len(github_token) > 4096:
        raise ValueError('账号、密码或 Token 长度超限')
    params = validate_build_parameters(body, config)
    from .testfairy import credentials as testfairy_credentials
    tf_user = str(body.get('testfairy_user') or '').strip()
    tf_password = str(body.get('testfairy_password') or '')
    if bool(tf_user) != bool(tf_password) or len(tf_user)>256 or len(tf_password)>256:
        raise ValueError('TestFairy 账号密码须同时填写，最长 256 字符')
    if tf_user and not params['testfairy']['enabled']:
        raise ValueError('请先启用 TestFairy，再填写账号密码')
    transient = {'testfairy': [tf_user, tf_password]} if tf_user else None
    if tf_user and params['testfairy']['save_session']:
        testfairy_credentials(params['testfairy']['login_url'], (tf_user, tf_password))
    # Persist credentials only after all build parameters are valid.
    if github_token:
        store_github_token(github_token)
    if user:
        store_credentials(user, password)
    doctor_check(config, strict=True)
    token_or_error(config)
    if active_count() >= parallel_limit(config):
        raise ValueError('已达到并发任务上限，请先等待或恢复已有任务')
    created = create_task(config, params)
    if transient:
        spawn(created['id'], wait=False, transient_credentials=transient)
    else:
        spawn(created['id'], wait=False)
    return public_task(task(created['id']))


def _field(body, key, maxlen):
    value = body.get(key, '')
    if not isinstance(value, str) or not value or len(value) > maxlen:
        raise ValueError(f'{key} 不能为空或超过 {maxlen} 字符')
    return value.strip()


def resume_build(task_id, body):
    from .cli import alive, doctor_check, spawn
    if not TASK_ID.fullmatch(task_id):
        raise ValueError('任务 ID 格式不合法')
    row = task(task_id)
    if row['status'] == 'SUCCESS':
        raise ValueError('任务已完成，无需恢复')
    if row['status'] in ('RUNNING', 'QUEUED') and alive(row['pid']):
        raise ValueError('原 Worker 仍在运行，禁止重复启动')
    cfg = load_config()
    doctor_check(cfg, strict=True)
    token_or_error(cfg)
    # The UI keeps a browser visible by default while replaying trace.
    update_task_params(task_id,
                       visible=(True if body.get('visible', True) is True else None),
                       verbose=(True if body.get('verbose', True) is True else None))
    previous = reserve_resume(cfg, task_id)
    try:
        spawn(task_id, wait=False)
    except Exception:
        update_task(task_id, status=previous['status'], pid=previous['pid'], note=previous['note'])
        raise
    return public_task(task(task_id))


def read_log(task_id, offset):
    task(task_id)  # verify known ID, never accept directory traversal
    if not TASK_ID.fullmatch(task_id):
        raise ValueError('任务 ID 格式不合法')
    path = task_folder(task_id) / 'run.log'
    if not path.is_file():
        return {'offset': 0, 'lines': '', 'size': 0}
    size = path.stat().st_size
    # Initial view shows recent data. Clients can incrementally tail via offset.
    if offset is None:
        position = max(0, size - MAX_LOG_BYTES)
    else:
        position = max(0, min(int(offset), size))
        if size - position > MAX_LOG_BYTES:
            position = size - MAX_LOG_BYTES
    with path.open('rb') as f:
        f.seek(position)
        data = f.read(MAX_LOG_BYTES)
        next_offset = f.tell()
    # safe_text redacts generic logged tokens from old versions as defense-in-depth.
    return {'offset': next_offset, 'lines': safe_text(data.decode('utf-8', errors='replace')),
            'size': size}


def get_screenshots(task_id):
    task(task_id)
    if not TASK_ID.fullmatch(task_id):
        raise ValueError('任务 ID 格式不合法')
    screen_dir = task_folder(task_id) / 'screenshots'
    if not screen_dir.is_dir():
        return []
    return sorted([f.name for f in screen_dir.iterdir()
                   if f.is_file() and not f.is_symlink() and IMAGE_FILE.fullmatch(f.name)])[:100]


# Read-only URL tests are separate from build tasks. Keep latest traces in
# memory so the page can follow SSO login and each Playwright detection step.
_DIAG_LOCK = threading.Lock()
_DIAG_JOBS: dict[str, dict] = {}


def start_diagnostic(url: str, *, number=None, expected='SUCCESS', site: str, expected_artifacts=None):
    from .diagnose import parse_target, run_diagnosis, validate_artifact_expectations
    expected_artifacts = validate_artifact_expectations(expected_artifacts)
    if expected_artifacts is not None and site != 'plugin':
        raise ValueError('版本/SHA 对比适用于 Plugin Jenkins 测试')
    config = load_config()
    target = parse_target(url, config, number=number)
    if target.site != site:
        raise ValueError(f'URL 是 {target.site.upper()} Jenkins，请使用对应的测试入口')
    if expected not in ('SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'):
        raise ValueError('预期结果无效')
    with _DIAG_LOCK:
        # Avoid spawning multiple live Chrome windows for the same Jenkins.
        if any(j['site'] == site and j['status'] == 'RUNNING' for j in _DIAG_JOBS.values()):
            raise ValueError(f'{site.upper()} Jenkins 已有正在执行的测试，请等待当前测试结束')
        now = __import__('time').monotonic()
        for key in list(_DIAG_JOBS):
            if now - _DIAG_JOBS[key]['created'] > 3600 and _DIAG_JOBS[key]['status'] != 'RUNNING':
                del _DIAG_JOBS[key]
        token = secrets.token_urlsafe(24)
        _DIAG_JOBS[token] = {'site': site, 'status': 'RUNNING', 'logs': [],
                             'report': None, 'created': now}

    def emit(line):
        with _DIAG_LOCK:
            row = _DIAG_JOBS.get(token)
            if row:
                row['logs'].append(safe_text(line))
                if len(row['logs']) > 200:
                    row['logs'] = row['logs'][-200:]

    def work():
        try:
            report = run_diagnosis(url, number=number, expected=expected, visible=True,
                                   config=config, emit=emit, expected_artifacts=expected_artifacts)
        except Exception as exc:
            emit(f'[DIAG-ERROR] {type(exc).__name__}: {safe_text(str(exc))}')
            report = {'verdict': 'UNKNOWN', 'actual': 'UNKNOWN', 'expected': expected,
                      'site': site, 'error': type(exc).__name__ + ': ' + safe_text(str(exc))}
        with _DIAG_LOCK:
            row = _DIAG_JOBS[token]
            row['report'] = report
            row['status'] = 'DONE'

    threading.Thread(target=work, daemon=True, name=f'jenkins-diag-{site}').start()
    return {'id': token, 'site': site, 'status': 'RUNNING'}


def poll_diagnostic(token):
    with _DIAG_LOCK:
        row = _DIAG_JOBS.get(token)
        if not row:
            raise KeyError('诊断任务不存在或已过期')
        return {'status': row['status'], 'site': row['site'], 'logs': list(row['logs']),
                'report': row['report']}


# On-demand *actionful* tests. Separate from the read-only URL diagnostics.
_LIVE_LOCK = threading.Lock()
_LIVE_JOBS: dict[str, dict] = {}


def start_live_test(body):
    from .live_tests import validate_options, gmx_build_test, validate_job_url
    kind = body.get('kind')
    if kind != 'gmx-build':
        raise ValueError('Bootstrap Test 已移除，请使用 Plugin Jenkins 单元测试或 GMX Jenkins Build Test')
    branch, country, project, settings = validate_options(body)
    job_url = body.get('job_url') or ''
    if kind == 'gmx-build':
        if body.get('confirm_trigger') is not True:
            raise ValueError('必须明确确认将真正触发 GMX Jenkins Build')
        if not isinstance(job_url, str):
            raise ValueError('Jenkins Job URL 格式无效')
        if job_url:
            validate_job_url(job_url, branch, load_config())
    with _LIVE_LOCK:
        if any(v['status']=='RUNNING' and v['kind']==kind and v['branch']==branch for v in _LIVE_JOBS.values()):
            raise ValueError(f'{branch} 的 {kind} 测试正在运行，不能重复触发')
        token = secrets.token_urlsafe(24)
        _LIVE_JOBS[token] = {'status':'RUNNING','kind':kind,'branch':branch,'logs':[],
                             'report':None,'created':__import__('time').monotonic()}

    logdir=DATA/'live-tests'/'logs'
    logdir.mkdir(parents=True,exist_ok=True)
    logfile=logdir/(token+'.log')
    def emit(message):
        clean = safe_text(message)
        with _LIVE_LOCK:
            with logfile.open('a',encoding='utf-8') as sink:
                sink.write(clean+'\n')
            row = _LIVE_JOBS.get(token)
            if row:
                row['logs'].append(clean)
                if len(row['logs'])>1000:
                    row['logs'] = row['logs'][-1000:]

    def work():
        from .telemetry import set_live_sink, set_verbose
        set_live_sink(emit)
        set_verbose(True)
        try:
            report = gmx_build_test(branch,country,project,settings,emit,job_url=job_url)
        except Exception as exc:
            emit(f'[ERROR] {type(exc).__name__}: {safe_text(str(exc))}')
            report = {'kind':kind, 'verdict':'FAIL','branch':branch,'error':safe_text(str(exc))}
        emit(f'[VERDICT] {report["verdict"]} · {kind} · {branch}')
        with _LIVE_LOCK:
            row=_LIVE_JOBS[token]
            row['status']='DONE'
            row['report']=report
        # Full log is already appended per event; UI retains a short rolling tail.
    threading.Thread(target=work,daemon=True,name=f'live-test-{kind}').start()
    return {'id':token,'kind':kind,'status':'RUNNING'}


def poll_live_test(token):
    with _LIVE_LOCK:
        row=_LIVE_JOBS.get(token)
        if not row:
            raise KeyError('现场测试任务不存在或服务已重启')
        return {'status':row['status'],'kind':row['kind'],'branch':row['branch'],
                'logs':list(row['logs']),'report':row['report']}


def make_handler(csrf_token):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'MacAppBuilder/' + __version__

        def log_message(self, fmt, *args):
            # Keep credentials out of request logging; no query strings printed.
            if os.environ.get('APP_BUILDER_WEB_DEBUG') == '1':
                print('[WEB]', safe_text(fmt % args), flush=True)

        def _allowed_host(self):
            # Strict Host validation defeats DNS rebinding against localhost.
            return self.headers.get('Host', '') == f'{HOST}:{self.server.server_port}'

        def _allowed_origin(self):
            origin = self.headers.get('Origin', '')
            return not origin or origin == f'http://{HOST}:{self.server.server_port}'

        def _headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def _send(self, data, status=200, typ='application/json; charset=utf-8'):
            b = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self._headers(status, typ, len(b))
            self.wfile.write(b)

        def _failed(self, status, message):
            self._send({'error': safe_text(message)}, status=status)

        def do_GET(self):
            if not self._allowed_host():
                return self._failed(403, '非法 Host')
            u = urlsplit(self.path)
            p = u.path
            try:
                if p == '/':
                    return self._asset('index.html')
                if p in ('/app.css', '/app.js', '/schedules.js'):
                    return self._asset(p[1:])
                if p == '/api/health':
                    return self._send({'app': 'mac-app-builder', 'service': 'web-console', 'version': __version__})
                if p == '/api/bootstrap':
                    config = load_config()
                    return self._send({'csrf': csrf_token,
                        'config': form_defaults(config),
                        'doctor': get_doctor()})
                if p == '/api/tasks':
                    return self._send({'tasks': [public_task(r) for r in task_list()]})
                m = re.fullmatch(r'/api/tasks/([^/]+)/log', p)
                if m:
                    offset = parse_qs(u.query).get('offset', [None])[0]
                    if offset is not None and (not offset.isascii() or not offset.isdigit()):
                        raise ValueError('offset 必须是正整数')
                    return self._send(read_log(m[1], None if offset is None else int(offset)))
                m = re.fullmatch(r'/api/tasks/([^/]+)/screenshots', p)
                if m:
                    return self._send({'files': get_screenshots(m[1])})
                m = re.fullmatch(r'/api/tasks/([^/]+)/screenshots/([^/]+)', p)
                if m:
                    files = get_screenshots(m[1])
                    if m[2] not in files:
                        return self._failed(404, '截图不存在')
                    return self._send((task_folder(m[1]) / 'screenshots' / m[2]).read_bytes(),
                                      typ='image/png')
                match = re.fullmatch(r'/api/diagnose/status/([A-Za-z0-9_-]{24,64})', p)
                if match:
                    return self._send(poll_diagnostic(match[1]))
                match = re.fullmatch(r'/api/live-tests/status/([A-Za-z0-9_-]{24,64})', p)
                if match:
                    return self._send(poll_live_test(match[1]))
                delivery_match = re.fullmatch(r'/api/delivery-tests/([a-f0-9-]{16,64})', p)
                if delivery_match:
                    from .delivery_tests import poll
                    return self._send(poll(delivery_match[1]))
                if p == '/api/outlook':
                    from .outlook import OutlookAdapter
                    try:
                        return self._send(OutlookAdapter().probe())
                    except Exception as exc:
                        return self._failed(400, safe_text(str(exc)))
                if p == '/api/doctor':
                    return self._send(get_doctor())
                from .schedule_api import handle_get
                handled = handle_get(p, parse_qs(u.query))
                if handled is not None:
                    return self._send(handled[1], status=handled[0])
                self._failed(404, '未找到该页面')
            except (ValueError, KeyError, TypeError) as e:
                self._failed(400, str(e))
            except Exception as e:
                print('[WEB] GET 操作失败：', type(e).__name__, safe_text(str(e)), flush=True)
                self._failed(500, '读取失败，请查看启动终端')

        def _asset(self, filename):
            file = STATIC / filename
            mime = 'text/html; charset=utf-8' if filename == 'index.html' else ('text/css; charset=utf-8' if filename.endswith('.css') else 'application/javascript; charset=utf-8')
            return self._send(file.read_bytes(), typ=mime)

        def do_POST(self):
            if not self._allowed_host() or not self._allowed_origin():
                return self._failed(403, '跨站请求被拒绝')
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf_token):
                return self._failed(403, 'CSRF 验证失败，请刷新页面')
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self._failed(415, '仅支持 JSON 请求')
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if length < 0 or length > MAX_REQUEST_BYTES:
                    return self._failed(413, '请求内容过大或无长度')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('需要 JSON 对象')
                if self.path == '/api/delivery-tests/start':
                    from .delivery_tests import start
                    return self._send(start(body), status=202)
                if self.path == '/api/live-tests/start':
                    return self._send(start_live_test(body), status=202)
                if self.path == '/api/diagnose/start':
                    url = body.get('url')
                    num = body.get('build_number')
                    site = body.get('site')
                    if not isinstance(url, str) or site not in ('plugin', 'gmx'):
                        raise ValueError('请选择 Plugin 或 GMX 测试，并输入 Jenkins URL')
                    if num in (None, ''):
                        num = None
                    elif type(num) is not int:
                        raise ValueError('Build Number 必须为正整数')
                    return self._send(start_diagnostic(url, number=num,
                                      expected=body.get('expected', 'SUCCESS'), site=site,
                                      expected_artifacts=body.get('expected_artifacts')), status=202)
                if self.path == '/api/diagnose':
                    from .diagnose import run_diagnosis
                    raw = body.get('url')
                    if not isinstance(raw, str):
                        raise ValueError('请输入 Jenkins Job / Build URL')
                    number = body.get('build_number')
                    if number in (None, ''):
                        number = None
                    elif type(number) is not int:
                        raise ValueError('Build Number 必须是整数')
                    result = run_diagnosis(raw, number=number,
                                           expected=body.get('expected', 'SUCCESS'),
                                           visible=True, expected_artifacts=body.get('expected_artifacts'))
                    return self._send(result)
                if self.path == '/api/branches':
                    from .branches import list_branches
                    return self._send(list_branches(body, load_config()))
                if self.path == '/api/tasks':
                    return self._send({'task': create_build(body)}, status=201)
                m = re.fullmatch(r'/api/tasks/([^/?]+)/resume', self.path)
                if m:
                    return self._send({'task': resume_build(m[1], body)})
                from .schedule_api import handle_post
                handled = handle_post(self.path, body)
                if handled is not None:
                    return self._send(handled[1], status=handled[0])
                self._failed(404, '不支持该操作')
            except (ValueError, KeyError, TypeError) as e:
                self._failed(400, str(e))
            except Exception as e:
                print('[WEB] POST 操作失败：', type(e).__name__, safe_text(str(e)), flush=True)
                self._failed(500, '操作失败，请查看终端或运行 doctor')

    return Handler


def serve(port=8765, *, open_browser=True, server_factory=ThreadingHTTPServer):
    """Bind loopback, then ensure the independent scheduler is available."""
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        raise ValueError('Web 端口必须在 1024–65535 之间')
    token = secrets.token_urlsafe(32)
    with server_factory((HOST, port), make_handler(token)) as httpd:
        from .scheduler import SchedulerSupervisor
        supervisor = SchedulerSupervisor()
        supervisor.start()
        url = f'http://{HOST}:{httpd.server_port}'
        print(f'[WEB] Mac APP Builder v{__version__} | 本地管理页面 {url}', flush=True)
        print('[WEB] 按 Ctrl+C 关闭页面服务；已经启动的后台构建不会停止。', flush=True)
        if open_browser:
            # Avoid opening a browser before server exists.
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            print('\n[WEB] 页面服务已关闭；Worker 继续后台执行。', flush=True)

        finally:
            supervisor.stop()
