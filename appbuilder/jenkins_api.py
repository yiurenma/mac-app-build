"""Jenkins over its own HTTP API with a user API token (no browser, no SSO session).

ApiJenkins offers the same methods the shared build functions in jenkins.py call on a
JenkinsBrowser (wait_job, mode, request_build, resolve_queue_build, wait_result,
find_callback, ...), so ensure_bootstrap / formal_build / the receipt-v1 dispatch
identity work unchanged. Differences that matter:

* Builds are submitted with POST /build or /buildWithParameters. Requests that
  authenticate with an API token need no CSRF crumb (Jenkins CSRF documentation).
* Jenkins answers 201 with Location /queue/item/N/; that queue id is the dispatch
  receipt, exactly as the browser path records it from the form POST response.
* GMX parameters are checked against the job's parameter definitions before the POST,
  and the started build's parameters are read back and compared afterwards.
* Any authentication problem (401/403, redirect to an SSO page) raises TokenUnavailable.
  When it happens before a POST the receipt stays PREPARING, so the caller may retry the
  same step with the browser; after a POST the receipt decides, never a blind resubmit.
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from .jenkins import NeedsAttention, callback_number_from_text, final_result_from_console
from .telemetry import action, safe_text
from .versions import folder

RESULTS = ('SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT')
MAX_BYTES = 64 * 1024 * 1024


class TokenUnavailable(NeedsAttention):
    """The API token cannot be used here; nothing was submitted by this call."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # a redirect is usually an SSO login page; never follow it


class JenkinsHttp:
    def __init__(self, user, token, *, timeout=30, ca_bundle=None):
        context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
        self._auth = 'Basic ' + base64.b64encode(f'{user}:{token}'.encode()).decode()
        self.timeout = timeout

    def request(self, method, url, data=None, accept='application/json'):
        """Returns (status, final_url, headers, body_bytes). Never follows redirects."""
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise NeedsAttention('Jenkins 地址必须是不带账号密码的 HTTPS 地址')
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {'Authorization': self._auth, 'Accept': accept}
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise NeedsAttention('Jenkins 响应过大，停止读取')
                return response.status, response.geturl(), dict(response.headers.items()), raw
        except urllib.error.HTTPError as exc:
            return exc.code, url, dict(exc.headers.items()) if exc.headers else {}, b''
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise TokenUnavailable(f'无法连接 Jenkins（{type(exc).__name__}）') from None

    def get_json(self, url, tree=None):
        """(status, object). Authentication problems raise TokenUnavailable."""
        query = '?' + urllib.parse.urlencode({'tree': tree}) if tree else ''
        status, final, headers, raw = self.request('GET', url.rstrip('/') + '/api/json' + query)
        _auth_check(status)
        if status != 200:
            return status, None
        kind = {k.lower(): v for k, v in headers.items()}.get('content-type', '')
        if 'json' not in kind.lower():
            raise TokenUnavailable('Jenkins 返回的不是 JSON（可能被登录页拦截）')
        try:
            return status, json.loads(raw.decode('utf-8'))
        except ValueError:
            raise NeedsAttention('Jenkins JSON 无法解析') from None


def _auth_check(status):
    if status in (401, 403):
        raise TokenUnavailable(f'Jenkins 拒绝了 Token（HTTP {status}）：Token 无效、无权限，或公司不允许 Token 访问')
    if status in (301, 302, 303, 307, 308):
        raise TokenUnavailable('Jenkins 把 Token 请求重定向到登录页，公司可能只允许浏览器 SSO')


def jenkins_root(job_url):
    parts = urlsplit(job_url)
    prefix = parts.path.split('/job/', 1)[0]
    return f'{parts.scheme}://{parts.netloc}{prefix}/'


def who_am_i(http, job_url):
    status, info = http.get_json(jenkins_root(job_url) + 'whoAmI', tree='name,authenticated,anonymous')
    if status != 200 or not isinstance(info, dict):
        raise TokenUnavailable(f'无法确认 Token 身份（HTTP {status}）')
    if info.get('anonymous') or not info.get('authenticated') or info.get('name') in (None, '', 'anonymous'):
        raise TokenUnavailable('Jenkins 把 Token 请求当成了匿名用户')
    return info['name']


class _ConsoleResponse:
    """Just enough of a Playwright APIResponse for jenkins.read_full_console."""
    def __init__(self, status, url, headers, raw):
        self.status, self.url = status, url
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._raw = raw

    def text(self):
        return self._raw.decode('utf-8', 'replace')

    def dispose(self):
        self._raw = b''


class ApiJenkins:
    transport = 'api-token'

    def __init__(self, config, site, credentials, *, http=None):
        self.conf = config['jenkins']
        self.site = site
        self.visible = False
        self.http = http or JenkinsHttp(*credentials, timeout=int(self.conf.get('api_timeout_seconds', 30)),
                                        ca_bundle=self.conf.get('ca_bundle'))
        self.poll = max(10, int(self.conf.get('poll_seconds', 45)))
        self.result_poll = max(2, min(60, int(self.conf.get('result_poll_seconds', 5))))
        self.discover_poll = max(1, int(self.conf.get('discover_poll_seconds', 10)))
        self.build_dispatch_checkpoint = None
        self.build_expected_baseline = None
        self.build_strict_dispatch = False
        self.gmx_settings = None
        self._submitted = {}
        self._verified = set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    # ---- identity and reads -------------------------------------------------
    def probe(self, job_url):
        name = who_am_i(self.http, job_url)
        action('JENKINS-API', f'{self.site} Token 身份：{name}；以下 Jenkins 操作不打开浏览器')
        return name

    def visit(self, url, timeout=None):
        status, _ = self.http.get_json(url, tree='_class')
        return status

    def wait_job(self, job_url):
        timeout = 60 * float(self.conf.get('discover_timeout_minutes', 30))
        deadline = time.monotonic() + timeout
        start, attempts, last = time.monotonic(), 0, None
        action('JENKINS', f'{self.site} 等待 Job 创建: {job_url}（Token 方式，每 {self.discover_poll}s 检查一次）')
        while time.monotonic() < deadline:
            status, _ = self.http.get_json(job_url, tree='name')
            attempts, last = attempts + 1, status
            if status == 200:
                print(f'[Jenkins/{self.site}] Job 已发现: {job_url}（检查 {attempts} 次）', flush=True)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            action('WAIT', f'{self.site} Job 暂未就绪：HTTP {status}，累计等待 {int(time.monotonic()-start)}s')
            time.sleep(min(self.discover_poll, remaining))
        raise TimeoutError(f'{self.site} Jenkins Job 等待超时（最后 HTTP={last}，检查 {attempts} 次）。'
                           f'请确认 Jenkins 已扫描新分支、Job URL 正确且有访问权限: {job_url}')

    def _job(self, job_url):
        """(job, {name: definition}). Several queries, simplest last: on the GMX Jenkins the
        rich query came back with NO parameterDefinitions while the page already showed
        'Build with Parameters' and a minimal query (property[_class,parameterDefinitions[name]])
        listed all of them. One parameter type there (ValidatingString) seems to break the
        richer fields, so a definition missing details is still a definition."""
        queries = ('buildable,property[parameterDefinitions[_class,name,type,choices,defaultParameterValue[value]]]',
                   'buildable,property[_class,parameterDefinitions[_class,name,type,choices]]',
                   'buildable,property[_class,parameterDefinitions[_class,name]]')
        job, defs, seen = None, {}, []
        for tree in queries:
            status, got = self.http.get_json(job_url, tree=tree)
            if status != 200 or not isinstance(got, dict):
                if job is None:
                    raise NeedsAttention(f'{self.site} Job 读取失败（HTTP {status}）')
                continue
            job = job or got
            for holder in got.get('property') or []:
                if not isinstance(holder, dict):
                    continue
                seen.append(str(holder.get('_class', '?')).rsplit('.', 1)[-1])
                for d in holder.get('parameterDefinitions') or []:
                    if isinstance(d, dict) and isinstance(d.get('name'), str):
                        d = dict(d)
                        d.setdefault('type', str(d.get('_class', '')).rsplit('.', 1)[-1])
                        defs.setdefault(d['name'], d)
            if defs:
                break
        if not defs and seen:
            action('JENKINS', f'{self.site} Job 属性里没有参数定义；属性类型：{", ".join(dict.fromkeys(seen))}')
        return job, defs

    def mode(self, job_url):
        job, defs = self._job(job_url)
        detected = 'parameters' if defs else ('build' if job.get('buildable') else 'unknown')
        action('JENKINS', f'{self.site} Job 入口（Token 读取参数定义）：{detected}')
        return detected

    def wait_parameter_mode(self, job_url, minutes=12):
        end, start = time.monotonic() + minutes * 60, time.monotonic()
        while time.monotonic() < end:
            if self.mode(job_url) == 'parameters':
                return
            action('WAIT', f'{self.site} 等待参数定义出现，已等待 {int(time.monotonic()-start)}s')
            time.sleep(self.poll)
        raise NeedsAttention('初始化后 Job 仍没有参数定义，请检查 Job 和 Jenkinsfile')

    def build_numbers(self, job_url):
        status, job = self.http.get_json(job_url, tree='builds[number]{0,100}')
        if status != 200 or not isinstance(job, dict):
            raise NeedsAttention(f'{self.site} Build 列表读取失败（HTTP {status}）')
        return {b['number'] for b in job.get('builds') or [] if type(b.get('number')) is int}

    def queued_builds(self, job_url):
        status, queue = self.http.get_json(jenkins_root(job_url) + 'queue', tree='items[id,task[url]]')
        if status != 200 or not isinstance(queue, dict):
            return []
        return sorted(i['id'] for i in queue.get('items') or []
                      if type(i.get('id')) is int and (i.get('task') or {}).get('url', '').rstrip('/') == job_url.rstrip('/'))

    # ---- submit -----------------------------------------------------------
    def _gmx_values(self, defs, country, project):
        settings = self.gmx_settings or {}

        def need(name, kind=None):
            if name not in defs:
                raise NeedsAttention(f'GMX Job 没有参数 {name}，未派发')
            if kind and kind not in str(defs[name].get('type', '')):
                raise NeedsAttention(f'GMX 参数 {name} 不是{"开关" if kind == "Boolean" else kind}类型，未派发')
            return defs[name]

        def choose(name, value):
            choices = need(name).get('choices')
            if isinstance(choices, list) and value not in choices:
                raise NeedsAttention(f'{name} 的可选值里没有 {value}，未派发')
            return value

        def flag(name, value):
            if not isinstance(value, bool):
                raise NeedsAttention(f'{name} 必须是布尔值')
            need(name, 'Boolean')
            return 'true' if value else 'false'

        if not country:
            raise NeedsAttention('ENTITIES 为空，未派发')
        values = {'ENTITIES': choose('ENTITIES', country),
                  'SYNC_UPLOAD': flag('SYNC_UPLOAD', settings.get('SYNC_UPLOAD', True))}
        need('CUSTOM_FOLDER')
        values['CUSTOM_FOLDER'] = settings.get('CUSTOM_FOLDER') or folder(country, project)
        for name in ('SKIP_TEST', 'FORCE', 'KEEP_ALIVE'):
            if name in settings:
                values[name] = flag(name, settings[name])
        for name in ('BUILD_TYPE', 'SIGNING_REPO_BRANCH'):
            if name in settings:
                values[name] = choose(name, str(settings[name]))
        if 'BUILD_TYPE' in values and not values['BUILD_TYPE'].strip():
            raise NeedsAttention('BUILD_TYPE 不能为空，否则 GMX Build 会被禁用')
        action('FORM', f'GMX 参数（Token 方式，已按 Job 参数定义核对）ENTITIES={country} '
                       f'SYNC_UPLOAD={values["SYNC_UPLOAD"]} CUSTOM_FOLDER={values["CUSTOM_FOLDER"]}')
        return values

    def request_build(self, job_url, *, with_parameters, country='', project=''):
        from .dispatch_identity import build_number, queue_url
        root = urlsplit(job_url)
        callback = self.build_dispatch_checkpoint or (lambda **fields: None)
        before = set() if self.build_strict_dispatch else self.build_numbers(job_url)
        if self.build_expected_baseline is not None and set(before) != set(self.build_expected_baseline):
            callback(state='CONFLICT')
            raise NeedsAttention('提交前 Build 历史已变化，可能是自动或外部构建；未派发，不关联新增编号')
        data = {}
        if with_parameters:
            _, defs = self._job(job_url)
            if not defs:
                raise NeedsAttention('Job 没有参数定义，未派发')
            if self.site == 'gmx':
                data = self._gmx_values(defs, country, project)
            endpoint = 'buildWithParameters'
        else:
            endpoint = 'build'
        callback(state='DISPATCHING')
        action('DISPATCH', f'{self.site} Token 方式提交 POST {endpoint}；中断后按结果未知处理，禁止盲重发')
        try:
            status, _, headers, _ = self.http.request('POST', job_url.rstrip('/') + '/' + endpoint, data=data)
        except TokenUnavailable:
            callback(state='UNKNOWN')
            raise NeedsAttention('提交请求网络中断，Jenkins 是否已接受未知；保留回执，不自动重发') from None
        if status in (401, 403):
            callback(state='PREPARING')      # rejected before queueing: nothing was submitted
            _auth_check(status)
        location = {k.lower(): v for k, v in headers.items()}.get('location', '')
        target = urlsplit(urllib.parse.urljoin(job_url, location))
        match = re.search(r'/queue/item/(\d+)/?$', target.path)
        queue = (int(match[1]) if match and target.scheme == root.scheme and target.netloc == root.netloc
                 and not target.query and not target.fragment else None)
        expected = queue_url(job_url, queue)
        if not expected or target.path.rstrip('/') != urlsplit(expected).path.rstrip('/'):
            queue = None
        receipt = {'http_status': status, 'queue_id': queue}
        if queue:
            receipt['queue_url'] = expected
        direct = build_number(job_url, urllib.parse.urljoin(job_url, location)) if location else None
        if direct:
            receipt['build_url'] = job_url.rstrip('/') + '/' + str(direct) + '/'
        accepted = (queue or direct) and status in (201, 302, 303)
        callback(state='ACCEPTED' if accepted else 'UNKNOWN', **receipt)
        action('DISPATCH', f'{self.site} 构建 POST 返回 HTTP {status}；queue_id={queue}')
        if data:
            self._submitted[job_url] = dict(data)
        return before

    def resolve_queue_build(self, job_url, queue_id):
        from .dispatch_identity import build_number, queue_url, same_job
        url = queue_url(job_url, queue_id)
        if not url:
            raise NeedsAttention('队列查询目标或排队编号无效')
        deadline = time.monotonic() + 60 * max(0.001, min(180, float(self.conf.get('queue_timeout_minutes', 12))))
        while time.monotonic() < deadline:
            try:
                status, item = self.http.get_json(url, tree='id,cancelled,task[url],executable[number,url]')
            except TokenUnavailable:
                # Seen on the GB folder: the queue record answers 200 while waiting and 403 once
                # the build has started, although the token can read the job. The build carries
                # our queue id, so look it up there with the same token. Only if the job itself is
                # refused does the token count as unusable.
                found = self.find_build_by_queue_id(job_url, queue_id, strict=True)
                if found:
                    return found
                action('QUEUE', f'{self.site} 队列记录拒绝了 Token；Job 里还没有 queueId={queue_id} 的 Build，继续等待')
                time.sleep(min(max(0.01, self.poll), max(0, deadline - time.monotonic())))
                continue
            if status in (404, 410):
                raise NeedsAttention('本次队列记录已消失，无法确定 Build；保留回执，不猜编号或重新提交')
            if status != 200 or not isinstance(item, dict):
                raise NeedsAttention('队列查询返回异常 HTTP；保留回执，不重新提交')
            if type(item.get('id')) is not int or item['id'] != queue_id:
                raise NeedsAttention('队列响应编号与本次回执不一致；禁止关联 Build')
            if item.get('cancelled') is True:
                raise NeedsAttention('本次排队任务已取消；保留回执，不自动重新提交')
            task = item.get('task')
            if not isinstance(task, dict) or not same_job(task.get('url'), job_url):
                shown = safe_text(str((task or {}).get('url') if isinstance(task, dict) else task))[:300]
                raise NeedsAttention(f'队列响应 Job 与本次提交不一致；禁止跨 Job 关联（Jenkins 返回：{shown}；本次：{job_url}）')
            executable = item.get('executable')
            if executable is not None:
                number = build_number(job_url, executable.get('url')) if isinstance(executable, dict) else None
                if not number or type(executable.get('number')) is not int or executable['number'] != number:
                    raise NeedsAttention('队列 executable URL 或编号无效、跨 Job 或不一致')
                action('QUEUE', f'{self.site} 本次 queue {queue_id} 明确关联 Build #{number}')
                return dict(queue_id=queue_id, job_url=job_url, relation='executable',
                            build_url=job_url.rstrip('/') + '/' + str(number) + '/')
            action('QUEUE', f'{self.site} 本次 queue {queue_id} 仍在排队；只读等待，不重新提交')
            time.sleep(min(max(0.01, self.poll), max(0, deadline - time.monotonic())))
        raise NeedsAttention('等待本次队列对应 Build 超时；保留回执，Resume 继续同一队列，不重新提交')

    def find_build_by_queue_id(self, job_url, queue_id, strict=False):
        """The build of THIS job whose recorded queueId is ours, or None. Read-only.
        strict: an authentication refusal on the job is raised (TokenUnavailable)."""
        try:
            status, job = self.http.get_json(job_url, tree='builds[number,queueId]{0,100}')
        except TokenUnavailable:
            if strict:
                raise
            return None
        if status != 200 or not isinstance(job, dict):
            return None
        hits = [b['number'] for b in job.get('builds') or []
                if type(b.get('number')) is int and b.get('queueId') == queue_id]
        if len(hits) != 1:
            return None
        action('QUEUE', f'{self.site} 排队记录已不可用；Build #{hits[0]} 记录的 queueId={queue_id} 与回执一致，据此关联')
        return dict(queue_id=queue_id, job_url=job_url, relation='executable',
                    build_url=job_url.rstrip('/') + '/' + str(hits[0]) + '/')

    def detect_new_build(self, job_url, baseline, minutes=12):
        deadline, start = time.monotonic() + minutes * 60, time.monotonic()
        while time.monotonic() < deadline:
            found = sorted(self.build_numbers(job_url) - set(baseline))
            if len(found) > 1:
                raise NeedsAttention(f'发现多个新 Build {found}，无法确认哪个属于本次触发')
            if found:
                return found[0]
            action('WAIT', f'{self.site} 等待新 Build Number，已等待 {int(time.monotonic()-start)}s')
            time.sleep(self.poll)
        raise NeedsAttention(f'等待结束仍未发现 Build Number；不会自动重复提交，请核对目标 Job: {job_url}')

    # ---- results ----------------------------------------------------------
    def _verify_parameters(self, job_url, number):
        sent = self._submitted.get(job_url)
        if not sent or (job_url, number) in self._verified:
            return
        status, build = self.http.get_json(job_url.rstrip('/') + f'/{number}/', tree='actions[parameters[name,value]]')
        if status != 200 or not isinstance(build, dict):
            return                                   # not started yet; checked on a later poll
        actual = {}
        for holder in build.get('actions') or []:
            for p in (holder or {}).get('parameters') or []:
                if isinstance(p, dict) and isinstance(p.get('name'), str):
                    value = p.get('value')
                    actual[p['name']] = ('true' if value else 'false') if isinstance(value, bool) else str(value)
        wrong = sorted(name for name, value in sent.items() if actual.get(name) != value)
        if wrong:
            raise NeedsAttention(f'GMX Build #{number} 实际收到的参数与提交不一致：{", ".join(wrong)}；请在 Jenkins 核对')
        self._verified.add((job_url, number))
        action('FORM', f'GMX Build #{number} 实际参数已回读，与提交一致')

    def _console_tail(self, build_url):
        status, _, _, raw = self.http.request('GET', build_url + 'consoleText', accept='text/plain')
        _auth_check(status)
        return raw.decode('utf-8', 'replace')[-30000:] if status == 200 else ''

    def wait_result(self, job_url, number, minutes=None, *, watch_callback=False):
        deadline = time.monotonic() + 60 * float(minutes or self.conf.get('build_timeout_minutes', 180))
        start = time.monotonic()
        build_url = job_url.rstrip('/') + f'/{number}/'
        action('JENKINS', f'{self.site} Build #{number}：Token 方式每 {self.result_poll}s 读取构建状态')
        while time.monotonic() < deadline:
            self._verify_parameters(job_url, number)
            status, build = self.http.get_json(build_url, tree='building,result')
            if status == 200 and isinstance(build, dict) and build.get('building') is False and build.get('result') in RESULTS:
                tail = self._console_tail(build_url)
                ended = final_result_from_console(tail)
                if ended and ended != build['result']:
                    raise NeedsAttention(f'{self.site} Build #{number} 状态 {build["result"]} 与日志结尾 {ended} 不一致')
                action('JENKINS', f'{self.site} Build #{number} 终态: {build["result"]}')
                return build['result'], tail
            if watch_callback:
                child = self.find_callback(job_url, number)
                if child is not None:
                    return 'DELEGATED', str(child)
            action('WAIT', f'{self.site} Build #{number} 尚未结束；已等待 {int(time.monotonic()-start)}s')
            time.sleep(min(self.result_poll, max(0, deadline - time.monotonic())))
        raise TimeoutError(f'{self.site} Build #{number} 结果未确认，超出构建监控时间；请直接检查 {build_url}consoleText')

    def find_callback(self, job_url, source_number, *, deadline=None):
        status, job = self.http.get_json(
            job_url, tree='builds[number,displayName,description,actions[causes[shortDescription]]]{0,50}')
        if status != 200 or not isinstance(job, dict):
            return None
        cards = []
        for b in job.get('builds') or []:
            n = b.get('number')
            if type(n) is not int:
                continue
            lines = [f'#{n}']
            if b.get('displayName') and b['displayName'] != f'#{n}':
                lines.append(str(b['displayName']))
            if b.get('description'):
                lines.append(re.sub(r'<[^>]+>', ' ', str(b['description'])))
            for holder in b.get('actions') or []:
                for cause in (holder or {}).get('causes') or []:
                    if isinstance(cause, dict) and cause.get('shortDescription'):
                        lines.append(str(cause['shortDescription']))
            cards.append('\n'.join(line.replace('\n', ' ') if i else line for i, line in enumerate(lines)))
        return callback_number_from_text('\n'.join(cards), source_number)

    def api_console(self, url):
        status, final, headers, raw = self.http.request('GET', url, accept='text/plain')
        _auth_check(status)
        return _ConsoleResponse(status, final, headers, raw)
