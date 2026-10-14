"""Read an APP join link from one exact Jenkins build's HTML console chain.

This module never triggers a build or opens TestFairy. HTML is necessary because
Jenkins build link labels may be shortened while their href retains the identity.
The original consoleText reader remains separate for Nexus and status checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from .jenkins import NeedsAttention, page_error_kind
from .telemetry import action

_TARGET = re.compile(r'\[Pipeline\]\s*build\s*\([^\n)]*»\s*upload-to-testfairy\s*»\s*master\s*\)')
_BUILD_START = re.compile(r'\[Pipeline\]\s*build(?=[\s(]|$)')
_COMPLETED = re.compile(r'\bcompleted:\s*([A-Z_]+)\b', re.I)
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_SENSITIVE_QUERY = re.compile(r'token|password|passwd|secret|session|auth|credential|api.?key|access.?key|^(?:code|key|sig|signature)$', re.I)
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
_BREAK = {'br', 'div', 'p', 'pre', 'li', 'section', 'tr', 'h1', 'h2'}
_IGNORE = {'script', 'style', 'template', 'noscript'}


@dataclass
class _Anchor:
    href: str
    start: int
    end: int = 0


class _Html(HTMLParser):
    """Keep text positions and hrefs together; never serialize DOM to a log."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.length = 0
        self.stack = []
        self.anchors = []
        self.active_anchors = []
        self.scopes = []
        self.active_scopes = []
        self.ignored = 0
        self.password_form = False
        self.feed(html)
        self.close()
        for anchor in self.active_anchors:
            anchor.end = self.length
        for _tag, _depth, start in self.active_scopes:
            self.scopes.append((start, self.length))
        self.text = ''.join(self.parts)

    def _append(self, value):
        if value and not self.ignored:
            self.parts.append(value)
            self.length += len(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _IGNORE:
            self.ignored += 1
        if not self.ignored:
            if tag in _BREAK:
                self._append('\n')
            if tag == 'input' and (attrs.get('type') or '').lower() == 'password':
                self.password_form = True
            if 'console-output' in (attrs.get('class') or '').split() or attrs.get('id') == 'out':
                self.active_scopes.append((tag, len(self.stack) + 1, self.length))
            if tag == 'a':
                anchor = _Anchor(attrs.get('href') or '', self.length)
                self.anchors.append(anchor)
                self.active_anchors.append(anchor)
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _IGNORE and self.ignored:
            self.ignored -= 1
        elif not self.ignored:
            if tag == 'a' and self.active_anchors:
                self.active_anchors.pop().end = self.length
            if (self.active_scopes and self.active_scopes[-1][0] == tag
                    and self.active_scopes[-1][1] == len(self.stack)):
                _tag, _depth, start = self.active_scopes.pop()
                self.scopes.append((start, self.length))
            if tag in _BREAK:
                self._append('\n')
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        self._append(data)

    def console(self):
        # A missing console is an error/login/unsupported page, never log data.
        outer = [(start, end) for start, end in self.scopes
                 if not any(a <= start and b >= end and (a, b) != (start, end)
                            for a, b in self.scopes)]
        outer = sorted(set(outer))
        if self.password_form or len(outer) != 1:
            raise NeedsAttention('Jenkins HTML 未显示唯一 Console Output，可能仍需登录或页面结构已变化')
        start, end = outer[0]
        if not self.text[start:end].strip():
            raise NeedsAttention('指定 Build 的 Console Output 为空')
        anchors = [_Anchor(a.href, a.start - start, a.end - start)
                   for a in self.anchors if start <= a.start <= a.end <= end]
        return self.text[start:end], anchors


def _decoded(value):
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded
    return value


def _url_parts(raw, *, query=False):
    """Validate before any navigation; never echo rejected untrusted URLs."""
    if (not isinstance(raw, str) or not raw or raw != raw.strip()
            or re.search(r'[\s\\\x00-\x1f\x7f]', raw)
            or re.search(r'%(?![0-9A-Fa-f]{2})', raw)):
        raise NeedsAttention('链接不是完整、合法的 HTTPS URL')
    decoded = _decoded(raw)
    if ('…' in decoded or '...' in decoded or re.search(r'[\\\x00-\x1f\x7f]', decoded)):
        raise NeedsAttention('链接含省略号、截断或不安全字符，不能采用')
    try:
        value = urlsplit(raw)
        if (value.scheme != 'https' or not value.hostname or value.username is not None
                or value.password is not None or value.fragment or '#' in raw
                or (value.query and not query) or ('?' in raw and not query)
                or value.port not in (None, 443)):
            raise ValueError
    except ValueError:
        raise NeedsAttention('链接必须是无凭据的完整 HTTPS 地址') from None
    if any(part in ('.', '..') for part in _decoded(value.path).split('/')):
        raise NeedsAttention('链接路径不安全')
    return value


def _job_url(raw):
    value = _url_parts(raw)
    path = value.path.rstrip('/') + '/'
    # Context paths are allowed; /job/name pairs must continue to the leaf job.
    _prefix, sep, jobs = path.partition('/job/')
    if (not sep or not jobs or not re.fullmatch(r'[^/]+(?:/job/[^/]+)*/', jobs)
            or '//' in path):
        raise NeedsAttention('GMX 来源必须是完整 Jenkins Job URL')
    return urlunsplit((value.scheme, value.netloc, path, '', ''))


def _build(job_url, number):
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise NeedsAttention('缺少有效的固定 GMX Build 编号，禁止猜测最近构建')
    job = _job_url(job_url)
    return {'job_url': job, 'number': number, 'build_url': job + str(number) + '/'}


def source_build(params, runtime):
    """Resolve reused job and build as one immutable pair, or the current pair."""
    if 'app_reuse' in runtime and runtime['app_reuse'] is not None:
        record = runtime['app_reuse']
        if not isinstance(record, dict):
            raise NeedsAttention('APP 复用来源记录无效，不能混用当前 Job 与历史编号')
        source = _build(record.get('job_url'), record.get('build_number'))
        if record.get('build_url') is not None and record['build_url'] != source['build_url']:
            raise NeedsAttention('APP 复用的 Job、Build 编号和 Build URL 不一致')
        return source
    number = runtime.get('gmx_build_number', runtime.get('gmx_formal_number'))
    return _build(params.get('gmx_job'), number)


def _same_origin(first, second):
    a, b = _url_parts(first), _url_parts(second)
    return (a.scheme, a.hostname, a.port or 443) == (b.scheme, b.hostname, b.port or 443)


def _resolve_href(raw, base):
    if (not isinstance(raw, str) or not raw or raw != raw.strip()
            or re.search(r'[\s\\\x00-\x1f\x7f]', raw)):
        raise NeedsAttention('Jenkins href 含不安全字符或不是完整链接')
    absolute = urljoin(base, raw)
    _url_parts(absolute)
    return absolute


def _downstream(raw, console_url):
    absolute = _resolve_href(raw, console_url)
    value = _url_parts(absolute)
    if not _same_origin(absolute, console_url):
        raise NeedsAttention('下游 Build 链接离开当前 Jenkins，已停止')
    match = re.fullmatch(r'(.*/job/upload-to-testfairy/job/master/)([1-9][0-9]*)/?', value.path)
    if not match:
        raise NeedsAttention('下游 href 不是 upload-to-testfairy/master 的完整 Build URL')
    return _build(urlunsplit((value.scheme, value.netloc, match[1], '', '')), int(match[2]))


def upload_build_from_html(html, console_url):
    """Select only the first target invocation, bounded before the next build."""
    _url_parts(console_url)
    text, anchors = _Html(html).console()
    first = _TARGET.search(text)
    if not first:
        raise NeedsAttention('完整日志未找到首个 » upload-to-testfairy » master) 构建段')
    next_build = _BUILD_START.search(text, first.end())
    end = next_build.start() if next_build else len(text)
    complete = _COMPLETED.search(text, first.end(), end)
    if complete is None:
        raise NeedsAttention('第一个 upload-to-testfairy/master 段缺少完成行，禁止借用后段结果')
    if complete[1].upper() != 'SUCCESS':
        raise NeedsAttention('第一个 upload-to-testfairy/master 段未 completed: SUCCESS')
    candidates = {}
    for anchor in anchors:
        if first.end() <= anchor.start and anchor.end <= complete.start():
            try:
                candidate = _downstream(anchor.href, console_url)
            except NeedsAttention:
                continue
            candidates[candidate['build_url']] = candidate
    if len(candidates) != 1:
        raise NeedsAttention('首个成功上传段中缺少唯一、完整的真实 Build href')
    result = next(iter(candidates.values()))
    completion_start = text.rfind('\n', first.end(), complete.start()) + 1
    completion_line = text[completion_start:complete.start()]
    build_label = re.search(r'\bBuild\s', completion_line)
    if not build_label:
        raise NeedsAttention('首个上传段缺少明确 Build completed 完成行')
    numbers = re.findall(r'#([1-9][0-9]*)\b', completion_line)
    if numbers and any(int(number) != result['number'] for number in numbers):
        raise NeedsAttention('首个上传段完成行的编号与真实 Build href 冲突')
    # A build number is unique only inside its own job. Never let the valid
    # Starting building href hide an invalid/different completion-line href.
    completion_anchors = [anchor for anchor in anchors
                          if anchor.end > completion_start + build_label.start()
                          and anchor.start < complete.start()]
    for anchor in completion_anchors:
        try:
            finished = _downstream(anchor.href, console_url)
        except NeedsAttention:
            raise NeedsAttention('首个上传段完成行的真实 href 不属于目标上传 Build') from None
        if finished['build_url'] != result['build_url']:
            raise NeedsAttention('首个上传段完成行与开始行的完整 Build 身份冲突')
    if not completion_anchors and not numbers:
        raise NeedsAttention('首个上传段完成行缺少可核对的 Build 身份')
    return result


def validate_join_url(value):
    """Validate a complete public join link without disclosing its share token."""
    url = _url_parts(value, query=True)
    host = url.hostname.lower()
    if ((host != 'testfairy.com' and not host.endswith('.testfairy.com'))
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in host.split('.'))):
        raise NeedsAttention('APP 链接必须位于 testfairy.com 或其租户子域')
    if not re.fullmatch(r'/join/[A-Za-z0-9_-]+/?', url.path):
        raise NeedsAttention('APP 链接缺少完整的 TestFairy /join/ 标识')
    if any(_SENSITIVE_QUERY.search(_decoded(key)) for key, _ in parse_qsl(url.query, keep_blank_values=True)):
        raise NeedsAttention('APP 链接含敏感认证查询参数，拒绝保存或展示')
    return urlunsplit(('https', host, url.path, url.query, ''))


def join_url_from_html(html, console_url):
    _url_parts(console_url)
    text, anchors = _Html(html).console()
    candidates = set()
    # Real hrefs take precedence over labels. Never treat a rejected href's
    # misleading label as independent URL evidence.
    for anchor in anchors:
        try:
            candidates.add(validate_join_url(anchor.href))
        except NeedsAttention:
            pass
    # Avoid a per-character copy of potentially very large complete logs.
    spans = sorted((anchor.start, anchor.end) for anchor in anchors)
    index = 0
    for match in _URL.finditer(text):
        while index < len(spans) and spans[index][1] <= match.start():
            index += 1
        if index < len(spans) and spans[index][0] < match.end():
            continue
        try:
            raw = match.group()
            # Only trim prose delimiters from a path. Query characters may
            # carry meaning, so preserve a complete query byte for byte.
            if '?' not in raw:
                raw = raw.rstrip('),;}')
            candidates.add(validate_join_url(raw))
        except NeedsAttention:
            pass
    if len(candidates) != 1:
        raise NeedsAttention('下游完整日志未找到唯一、完整的 testfairy.com/join APP 链接')
    return next(iter(candidates))


def _exact_target(actual, target):
    try:
        a, b = _url_parts(actual), _url_parts(target)
        return (_same_origin(actual, target) and a.path.rstrip('/') == b.path.rstrip('/'))
    except NeedsAttention:
        return False


def _navigation_href(html, base, label, target, *, required=False):
    document = _Html(html)
    links = [a for a in document.anchors
             if ' '.join(document.text[a.start:a.end].split()).casefold() == label.casefold()]
    if not links:
        if required:
            raise NeedsAttention('Jenkins 页面缺少所需导航链接')
        return None
    targets = set()
    for anchor in links:
        absolute = _resolve_href(anchor.href, base)
        if not _exact_target(absolute, target):
            raise NeedsAttention('Jenkins 导航链接不属于当前固定 Build，已停止')
        targets.add(absolute)
    if len(targets) != 1:
        raise NeedsAttention('Jenkins 当前固定 Build 的导航链接不唯一')
    return targets.pop()


def lookup_app_link(browser, source_job_url, source_build_number, emit=None):
    """Shared read-only HTML navigation used by the formal run and diagnostics."""
    source = _build(source_job_url, source_build_number)
    report = emit if emit is not None else lambda message: action('APP-LINK', message)
    stage = 'source'

    def read(url, name):
        nonlocal stage
        stage = name
        report(f'[APP-LINK] {name}: 打开固定 Build 的 HTML 页面')
        status = browser.visit(url)
        if status != 200 or not _exact_target(browser.page.url, url):
            raise NeedsAttention('Jenkins HTML 页面不可读、需要登录或未返回固定 Build')
        if page_error_kind(browser.page):
            raise NeedsAttention('Jenkins HTML 页面报告错误或访问被拒绝')
        html = browser.page.content()
        if not isinstance(html, str) or not html.strip() or _Html(html).password_form:
            raise NeedsAttention('Jenkins HTML 为空或仍显示登录表单')
        report(f'[APP-LINK] {name}: HTTP=200，HTML字符数={len(html)}；不记录页面正文')
        return html

    try:
        report(f'[APP-LINK] source: 固定 GMX Build #{source["number"]}，仅只读查找')
        source_console = source['build_url'] + 'console'
        html = read(source_console, 'source_console')
        full = _navigation_href(html, source_console, 'Full log', source['build_url'] + 'consoleFull')
        if full is None:
            # Small Jenkins logs omit the Full log action. Request the same
            # build's standard HTML full-console endpoint, never consoleText.
            full = source['build_url'] + 'consoleFull'
            report('[APP-LINK] source_full: 页面未提供 Full log，读取同一固定 Build 的 HTML 完整日志')
        html = read(full, 'source_full')
        stage = 'first_upload_segment'
        downstream = upload_build_from_html(html, full)
        report(f'[APP-LINK] first_upload_segment: 第一个目标段 completed: SUCCESS；真实 href Build #{downstream["number"]}')
        html = read(downstream['build_url'], 'downstream_build')
        downstream_console = downstream['build_url'] + 'console'
        actual_console = _navigation_href(html, downstream['build_url'], 'Console Output', downstream_console)
        html = read(actual_console or downstream_console, 'downstream_console')
        full = _navigation_href(html, downstream_console, 'Full log', downstream['build_url'] + 'consoleFull')
        if full:
            html = read(full, 'downstream_full')
        else:
            report('[APP-LINK] downstream_full: 无 Full log 入口，使用当前 Console Output')
        stage = 'join_url'
        share = join_url_from_html(html, full or downstream_console)
        report('[APP-LINK] join_url: 已提取唯一完整 TestFairy /join/ 链接；分享标识和查询参数不写入日志')
        return {'share_url': share, 'source_job_url': source['job_url'],
                'source_build_number': source['number'], 'source_build_url': source['build_url'],
                'downstream_job_url': downstream['job_url'], 'downstream_build_number': downstream['number'],
                'downstream_build_url': downstream['build_url']}
    except Exception as exc:
        # Browser errors can contain URLs, DOM excerpts and credentials. Keep
        # raw causes out of caller-visible reports and persisted tracebacks.
        report(f'[APP-LINK] {stage}: 停止，未采用后续构建或截断链接')
        detail = str(exc) if isinstance(exc, NeedsAttention) and type(exc) is NeedsAttention else ''
        # Only our own constant messages are safe; browser.visit may itself
        # raise NeedsAttention with a raw URL, so never include its message.
        if stage in ('first_upload_segment', 'join_url'):
            raise NeedsAttention(f'APP 链接提取停止（{stage}）：{detail}') from None
        raise NeedsAttention(f'APP 链接提取停止（{stage}）；请检查固定 Build 的访问和 HTML 导航') from None


def extract_link(browser, params, runtime, emit=None):
    source = source_build(params, runtime)
    return lookup_app_link(browser, source['job_url'], source['number'], emit=emit)
