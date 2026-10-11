"""Jenkins automation through visible browser pages (NO Jenkins REST API).

Real corporate SSO and customized Jenkins DOM can differ. Unrecognized screens
raise NeedsAttention rather than clicking a potentially incorrect control.
"""
from __future__ import annotations
import os
import re
import time
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError
from .auth import read_credentials, approved_login_host, submit_recognized_login
from .versions import folder
from .store import patch_runtime, read_state
from .telemetry import action, debug, operation


class NeedsAttention(RuntimeError):
    pass


def page_error_kind(page):
    """Recognize error-page headings, never old messages inside console logs."""
    try:
        headings = page.locator('h1, h2, [role="alert"]').all_text_contents()
    except (AttributeError, PlaywrightError):
        headings = []
    headings = headings if isinstance(headings, list) else []
    try:
        title = page.title()
    except (AttributeError, PlaywrightError):
        title = ''
    for raw in [title, *headings]:
        if not isinstance(raw, str):
            continue
        text = ' '.join(raw.lower().split()).strip(' .!')
        text = re.sub(r'\s*[-|]\s*jenkins$', '', text).strip(' .!')
        if text in ('not found', '404 not found', 'oops! not found', 'oops not found'):
            return 'not-found'
        if text in ('access denied', 'forbidden', '403 forbidden', 'unauthorized', '401 unauthorized'):
            return 'denied'
    return None


def login_route(url):
    path = urlsplit(url).path.rstrip('/').lower()
    return (path.endswith(('/login', '/signin', '/j_security_check'))
            or '/securityrealm/' in path)


def has_login_inputs(page):
    selectors = 'input[type="password"],input[name="j_username"],input[name="loginfmt"]'
    if '/job/' not in urlsplit(page.url).path:
        selectors += ',input[name="username"],input[type="email"],input[autocomplete="username"]'
    count = page.locator(selectors).count()
    return isinstance(count, int) and count > 0


def explicit_acl_denial(page):
    """Only a concrete non-anonymous permission message proves an ACL denial."""
    try:
        text = page.locator('body').inner_text(timeout=1000)[:4000]
    except (AttributeError, TypeError, PlaywrightError):
        return False
    return bool(isinstance(text, str) and re.search(
        r'(?im)\b(?!anonymous\b)[\w.@-]+\s+is missing (?:the )?(?:Job|Overall|View|Run)/[\w]+ permission', text))


def same_origin_login_entry(page, target):
    """Follow only an actual Jenkins sign-in link, never invent another Job URL."""
    expected = urlsplit(target)
    prefix = expected.path.split('/job/', 1)[0].rstrip('/')
    allowed_paths = {prefix + '/login', prefix + '/signin', prefix + '/securityRealm/commenceLogin'}
    try:
        links = page.locator('a[href]').evaluate_all('(links)=>links.map(a=>a.getAttribute("href"))')
    except (AttributeError, PlaywrightError):
        return None
    if not isinstance(links, list):
        return None
    for href in links:
        if not isinstance(href, str):
            continue
        absolute = urljoin(page.url, href)
        candidate = urlsplit(absolute)
        if (candidate.scheme == 'https' and candidate.netloc == expected.netloc
                and not candidate.username and not candidate.password
                and candidate.path.rstrip('/') in allowed_paths):
            return absolute
    return None


def same_target_url(actual, target):
    """Compare saved URL identity without decoding encoded branch slashes."""
    current, expected = urlsplit(actual), urlsplit(target)
    return (current.scheme == expected.scheme and current.netloc == expected.netloc
            and current.path.rstrip('/') == expected.path.rstrip('/')
            and current.query == expected.query)


def configured_login_entry(conf, site, target):
    """Normal Jenkins login route from the configured origin/context only."""
    parent = urlsplit(conf.get(site + '_parent', ''))
    expected = urlsplit(target)
    if (parent.scheme != 'https' or parent.netloc != expected.netloc
            or parent.username or parent.password or '/job/' not in parent.path):
        return None
    prefix = parent.path.split('/job/', 1)[0].rstrip('/')
    if not expected.path.startswith(prefix + '/job/'):
        return None
    return parent.scheme + '://' + parent.netloc + prefix + '/login'


def final_result_from_console(text):
    """Recognize only the final Jenkins console status line, not quoted log text."""
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    if not end:
        return None
    from .nexus import normalize_console
    start = text.rfind('\n', 0, end) + 1
    last = normalize_console(text[start:end]).strip()
    match = re.fullmatch(r'Finished:\s*(SUCCESS|FAILURE|ABORTED|UNSTABLE|NOT_BUILT)',
                         last, re.I)
    return match[1].upper() if match else None


def result_from_status_markup(markup):
    """Recognize modern Jenkins build-header status icons, not arbitrary page text.

    Input MUST be markup from a build-status element or the build heading's
    status SVG. Never feed this function the entire job page: other historical
    build cards and sidebar icons might contain SUCCESS or FAILURE.
    """
    value = str(markup).lower()
    # Jenkins modern builds may use semantic success classes/accessible labels.
    # This parser is ONLY fed one status icon, never a whole page.
    if re.search(r'(?:data-(?:build-)?result|data-(?:build-)?status)=["\'](?:success|successful|passed)["\']', value):
        return 'SUCCESS'
    # Jenkins may encode status in SVG <use href=".../status/blue.svg#blue">
    # rather than the SVG class; ignore generic icons and unrelated page text.
    if re.search(r'icon-(?:running|progress|building|pending|queued|anime)\b|\b(?:building|running|progress)\b', value):
        return None
    for outcome, signature in (
        ('FAILURE', r'icon-(?:failure|red)\b|build-status-(?:failure|red)\b|(?:title|aria-label|alt)=["\']failure["\']|(?:status/|status%2f)(?:red|failure)(?:\.svg|[\#/?"\'])|[\#/]failure(?:["\']|\b)'),
        ('ABORTED', r'icon-aborted\b|build-status-aborted\b|(?:title|aria-label|alt)=["\']aborted["\']|(?:status/|status%2f)aborted(?:\.svg|[\#/?"\'])|[\#/]aborted(?:["\']|\b)'),
        ('UNSTABLE', r'icon-(?:unstable|yellow)\b|build-status-(?:unstable|yellow)\b|(?:title|aria-label|alt)=["\']unstable["\']|(?:status/|status%2f)(?:yellow|unstable)(?:\.svg|[\#/?"\'])|[\#/]unstable(?:["\']|\b)'),
        ('SUCCESS', r'icon-(?:success|green|blue)\b|(?:jenkins-)?(?:build-)?status-(?:success|green|blue)\b|(?:title|aria-label|alt)=["\'](?:success|successful|passed)["\']|(?:status/|status%2f)(?:blue|green|success)(?:\.svg|[\#/?"\'])|[\#/]success(?:["\']|\b)'),
    ):
        if re.search(signature, value):
            return outcome
    return None


def status_from_icon_colors(colors):
    """Conservative CSS-color fallback for a small status icon in the exact build row.

    The caller MUST verify the specific build link, icon visibility, and size;
    never classify generic green text/charts or another build's icon.
    """
    seen = set()
    for color in colors:
        match = re.fullmatch(r'rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*([\d.]+))?\s*\)', str(color).lower())
        if not match:
            continue
        r, g, b = map(int, match.group(1,2,3))
        alpha = float(match[4]) if match[4] is not None else 1.0
        if alpha < 0.5:
            continue
        # Jenkins theme greens & reds; do not infer blue/yellow/gray.
        if g >= 92 and g >= r * 1.45 and g >= b * 1.2 and r <= 145:
            seen.add('SUCCESS')
        elif r >= 145 and r >= g * 1.5 and r >= b * 1.3:
            seen.add('FAILURE')
    return next(iter(seen)) if len(seen) == 1 else None


def browser_launch_options(config, *, headless: bool):
    """Use an IT-managed system browser or the Playwright-managed Chromium."""
    channel = config.get('jenkins', {}).get('browser_channel', 'chromium')
    if channel not in ('chromium', 'chrome', 'msedge'):
        raise NeedsAttention(f'无效的 jenkins.browser_channel: {channel}')
    options = {'headless': headless}
    if channel != 'chromium':
        options['channel'] = channel
    return options


def saved_auth_path(data_dir: Path, site: str):
    return data_dir / 'auth' / (site + '.json')


def login_parent(conf, site):
    return conf[site + '_parent'].rstrip('/').removesuffix('/job') + '/'


def interactive_login(config, data_dir: Path, site: str):
    parent = login_parent(config['jenkins'], site)
    with sync_playwright() as p:
        browser = p.chromium.launch(**browser_launch_options(config, headless=False))
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(parent, wait_until='domcontentloaded', timeout=60000)
        print(f'请在弹出的浏览器中登录 {site} Jenkins。\n登录后回到终端按 Enter 保存认证状态。')
        input('完成登录后按 Enter：')
        path = saved_auth_path(data_dir, site)
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(path))
        path.chmod(0o600)
        print('已保存浏览器会话数据（请当作敏感文件保护）：', path)
        ctx.close()
        browser.close()


class JenkinsBrowser:
    def __init__(self, config, data_dir: Path, task_dir: Path, site: str, *, visible=False, verbose=False, manual_login=False):
        self.conf = config['jenkins']
        self.site = site
        self.visible = bool(visible)
        self.manual_login = bool(manual_login)
        self.verbose = bool(verbose)
        self.task_dir = task_dir
        self.screens = task_dir / 'screenshots'
        self.screens.mkdir(parents=True, exist_ok=True)
        auth = saved_auth_path(data_dir, site)
        self.auth_path = auth
        self.credentials = read_credentials()
        if not auth.exists() and not self.credentials and not self.manual_login:
            raise NeedsAttention('尚未配置 Jenkins 登录，请在首次 run 中提供 '
                                 '--jenkins-user 和 --jenkins-password')
        self.playwright = sync_playwright().start()
        launch_opts = browser_launch_options(config, headless=not self.visible)
        if self.visible:
            launch_opts['slow_mo'] = 300  # make browser clicks visibly observable, not a stealth run
        operation('PW', f'chromium.launch(channel={launch_opts.get("channel", "chromium")}, headless={launch_opts["headless"]}, slow_mo={launch_opts.get("slow_mo", 0)})')
        self.browser = self.playwright.chromium.launch(**launch_opts)
        action('BROWSER', f'{site} Chrome 已启动（{"可见窗口" if self.visible else "后台模式"}）')
        operation('PW', f'browser.new_context(storage_state={"本地登录缓存" if auth.exists() else "新会话"}, accept_downloads=False)')
        self.context = (self.browser.new_context(storage_state=str(auth), accept_downloads=False)
                        if auth.exists() else self.browser.new_context(accept_downloads=False))
        operation('PW', 'context.new_page()')
        self.page = self.context.new_page()
        self.page.set_default_timeout(12000)
        self.poll = max(10, int(self.conf.get('poll_seconds', 45)))
        self.result_poll = max(2, min(60, int(self.conf.get('result_poll_seconds', 5))))
        # Branch jobs are created asynchronously after a Git push. Keep this
        # discovery cadence independent from long-running Build monitoring.
        self.discover_poll = max(1, int(self.conf.get('discover_poll_seconds', 10)))

    def close(self):
        operation('PW', 'context.close(); browser.close(); playwright.stop()')
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def __enter__(self):
        return self

    def __exit__(self, typ, val, tb):
        if typ:
            try:
                operation('PW', f'page.screenshot({self.site}-error.png, full_page=False)')
                self.page.screenshot(path=str(self.screens / f'{self.site}-error.png'), full_page=False)
            except Exception:
                pass
        self.close()

    def _cache_login(self):
        """Atomic write; independent workers must never see a partial JSON."""
        if getattr(self, '_defer_login_cache', False):
            return
        path = self.auth_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.jenkins-', suffix='.json', dir=path.parent)
        os.close(fd)
        try:
            self.context.storage_state(path=temp)
            os.chmod(temp, 0o600)
            os.replace(temp, path)
            action('AUTH', f'{self.site} 登录会话已更新，后续可直接复用')
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _wait_manual_login(self, expected_url):
        """Allow bounded manual SSO in a visible browser; never click Build."""
        from .auth import approved_login_host
        host = urlsplit(expected_url).hostname
        extra = self.conf.get('auth_allowed_hosts', ['*.hsbc'])
        if not approved_login_host(self.page.url, expected_url, extra):
            raise NeedsAttention(f'{self.site} 登录跳转到未批准主机：{urlsplit(self.page.url).hostname}')
        action('AUTH', f'{self.site} 浏览器需要登录：请在弹出的 Chrome 窗口完成公司 SSO/MFA（最多 180 秒）')
        limit = time.monotonic() + 180
        while time.monotonic() < limit:
            try:
                current = urlsplit(self.page.url)
                # Only the Jenkins host, outside its login screen, is accepted.
                path = current.path.rstrip('/').lower()
                on_jenkins = current.hostname == host and not login_route(self.page.url)
                has_form = has_login_inputs(self.page)
                if on_jenkins and not has_form and not page_error_kind(self.page):
                    self._cache_login()
                    action('AUTH', f'{self.site} 人工 SSO 已完成，返回原 Jenkins URL 检查')
                    return True
            except PlaywrightError:
                pass  # SSO navigation is in flight.
            time.sleep(1)
        raise NeedsAttention(f'{self.site} 诊断登录等待超过 180 秒；请在 Chrome 中完成公司 SSO 再试')

    def _login_if_needed(self, expected_url):
        host = urlsplit(expected_url).hostname
        redirected = urlsplit(self.page.url).hostname != host
        password_page = self.page.locator('input[type="password"]').count() > 0
        login_input = (self.page.locator('input[name="j_username"]').count() > 0
                       or self.page.locator('input[name="loginfmt"]').count() > 0)
        on_login_route = login_route(self.page.url)
        if not (redirected or password_page or login_input or on_login_route or has_login_inputs(self.page)):
            return False
        if getattr(self, 'manual_login', False):
            return self._wait_manual_login(expected_url)
        if not self.credentials and getattr(self, 'visible', False):
            return self._wait_manual_login(expected_url)
        if not self.credentials:
            raise NeedsAttention(f'{self.site} 会话已过期且未配置 Keychain 认证；'
                                 f'请运行 ./app-build login --site {self.site} 完成登录后 Resume 原任务')
        extra = self.conf.get('auth_allowed_hosts', ['*.hsbc'])
        if not approved_login_host(self.page.url, expected_url, extra):
            raise NeedsAttention(f'{self.site} 登录跳转到未获批准域名：'
                                 f'{urlsplit(self.page.url).hostname}。'
                                 '请先核实域名，再加入 config.yaml 的 jenkins.auth_allowed_hosts')
        username, password = self.credentials
        action('AUTH', f'{self.site} 检测到登录页面，尝试自动填写账号密码')
        if not submit_recognized_login(self.page, username, password,
                                       log=lambda m: operation('PW', m)):
            if getattr(self, 'visible', False):
                return self._wait_manual_login(expected_url)
            raise NeedsAttention(f'{self.site} 登录页控件无法安全识别；'
                                 '请检查公司 SSO 页面或改用 --live 人工处理')
        try:
            self.page.wait_for_url(lambda u: urlsplit(str(u)).hostname == host and not login_route(str(u)),
                                   timeout=25000)
        except PlaywrightTimeout:
            if not self.visible:
                raise NeedsAttention(f'{self.site} SSO 登录后未返回 Jenkins，'
                                     '可能需要 MFA；请用 --live 运行以查看认证过程')
            action('AUTH', f'{self.site} 等待 SSO/MFA 完成，最多 90 秒')
            try:
                self.page.wait_for_url(lambda u: urlsplit(str(u)).hostname == host and not login_route(str(u)),
                                       timeout=90000)
            except PlaywrightTimeout as e:
                raise NeedsAttention(f'{self.site} SSO/MFA 未完成，需要人工检查') from e
        # Verify no password form remained; only then save state.
        if (self.page.locator('input[type="password"]').count()
                or login_route(self.page.url) or has_login_inputs(self.page)):
            raise NeedsAttention(f'{self.site} 仍停留在认证页面，可能是密码错误或额外验证')
        # A post-login error page does not prove that authentication succeeded.
        # Reload the exact target for its status, but do not overwrite the cache.
        if not page_error_kind(self.page):
            self._cache_login()
        return True

    def _recover_session(self, target, entry, timeout):
        """Worker equivalent of login: fresh context, parent, normal auth, target.

        No interactive input(), no subprocess, no deletion of the prior cache.
        Only a verified target response can replace that cache.
        """
        old_context, old_page = self.context, self.page
        fresh = self.browser.new_context(accept_downloads=False)
        try:
            self.context = fresh
            self.page = fresh.new_page()
            self.page.set_default_timeout(12000)
            self._defer_login_cache = True
            # Use exactly the parent destination used by the interactive CLI.
            parent = login_parent(self.conf, self.site) if configured_login_entry(self.conf, self.site, target) else entry
            response = self.page.goto(parent, wait_until='domcontentloaded', timeout=timeout)
            authenticated = self._login_if_needed(target)
            if not authenticated and not explicit_acl_denial(self.page):
                # Some Jenkins error shells omit login controls even on parent.
                self.page.goto(entry, wait_until='domcontentloaded', timeout=timeout)
                self._login_if_needed(target)
            response = self.page.goto(target, wait_until='domcontentloaded', timeout=timeout)
            if (login_route(self.page.url) or has_login_inputs(self.page)
                    or not same_target_url(self.page.url, target)):
                raise NeedsAttention(f'{self.site} 自动登录未返回精确原目标，保留原登录缓存；请检查 Chrome 中的公司验证或跳转')
            self._defer_login_cache = False
            if response and response.status == 200 and not page_error_kind(self.page):
                self._cache_login()
                action('AUTH', f'{self.site} 新会话登录成功，自动继续原任务当前阶段')
        except Exception:
            self.context, self.page = old_context, old_page
            fresh.close()
            raise
        finally:
            self._defer_login_cache = False
        old_context.close()
        return response

    def visit(self, url, timeout=60000):
        operation('PW', f'page.goto(url={url}, wait_until=domcontentloaded, timeout={timeout})')
        started = time.monotonic()
        debug(f'[BROWSER/{self.site}] navigate {url}')
        try:
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        except PlaywrightTimeout as e:
            raise NeedsAttention(f'Jenkins 页面访问超时: {url}') from e
        # Error shells may omit every sign-in control. Saved credentials can
        # still use the configured Jenkins login route once; this does not
        # establish authorization and never overrides a concrete ACL denial.
        recovered = False
        status = response.status if response else None
        kind = page_error_kind(self.page)
        if status in (401, 403) or (status == 200 and kind):
            entry = same_origin_login_entry(self.page, url)
            if not entry and status in (401, 403) and self.credentials:
                entry = configured_login_entry(self.conf, self.site, url)
            attempted = getattr(self, '_login_recovery_targets', set())
            if entry and url not in attempted and not explicit_acl_denial(self.page):
                self._login_recovery_targets = attempted | {url}
                action('AUTH', f'{self.site} HTTP {status} 错误页可能是会话失效；通过同域 Jenkins 登录入口仅恢复一次，不修改目标 Job URL')
                response = self._recover_session(url, entry, timeout)
                recovered = True
        # Fresh recovery already verified and revisited the exact target.
        authenticated = False if recovered else self._login_if_needed(url)
        if authenticated:
            operation('PW', f'page.goto({url}) 登录后重新加载目标 Job')
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            if (urlsplit(self.page.url).hostname != urlsplit(url).hostname
                    or login_route(self.page.url) or has_login_inputs(self.page)):
                raise NeedsAttention(f'{self.site} 认证后仍未返回 Jenkins Job 页面')
        code = response.status if response else None
        if code in (401, 403, 404) or page_error_kind(self.page):
            final = urlsplit(self.page.url)
            action('JENKINS-ACCESS', f'{self.site} HTTP={code}; 页面类型={page_error_kind(self.page) or "unknown"}; 最终路径={final.scheme}://{final.netloc}{final.path}; 登录恢复次数={int(url in getattr(self, "_login_recovery_targets", set()))}')
        operation('PW', f'page.goto completed: HTTP={code}, elapsed={time.monotonic()-started:.1f}s, title={self.page.title()}')
        debug(f'[BROWSER/{self.site}] HTTP {code} page={self.page.title()}')
        return code

    def wait_job(self, job_url):
        timeout = 60 * float(self.conf.get('discover_timeout_minutes', 30))
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        action('JENKINS', f'{self.site} 等待 Job 创建: {job_url}（每 {self.discover_poll}s 检查一次）')
        last_http = None
        attempts = 0
        while time.monotonic() < deadline:
            http = self.visit(job_url)
            last_http = http
            attempts += 1
            error_kind = page_error_kind(self.page)
            if http == 200 and not error_kind:
                print(f'[Jenkins/{self.site}] Job 已发现: {job_url}（检查 {attempts} 次）', flush=True)
                return
            if http in (401, 403) or error_kind == 'denied':
                reason = ('页面明确报告缺少 Jenkins 权限，停止自动操作' if explicit_acl_denial(self.page)
                          else '登录可能过期，也可能 Job 不存在/未索引或权限不足，不能仅凭此页面确定原因')
                parent = job_url.rstrip('/').rsplit('/job/', 1)[0] + '/'
                raise NeedsAttention(f'Jenkins Job 暂不可访问 (HTTP {http})：{reason}。'
                                     f'目标: {job_url}；请用同一公司账号在 Chrome 核对父 Job: {parent}。'
                                     f'如需重新登录，运行 ./app-build login --site {self.site} 后 Resume 原任务；不新建分支、不改服务器权限')
            # A 404 is expected while branch indexing has not created the job.
            # Retry only until the configured discovery timeout and never click
            # Build while Jenkins reports Not Found.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(self.discover_poll, remaining)
            action('WAIT', f"{self.site} Job 暂未就绪：HTTP {http} / {error_kind or '未识别页面'}，累计等待 {int(time.monotonic()-start)}s，{delay:g}s 后重试（第 {attempts} 次检查）")
            time.sleep(delay)
        raise TimeoutError(
            f'{self.site} Jenkins Job 等待超时（最后 HTTP={last_http}，检查 {attempts} 次）。'
            f'请确认 Jenkins 已扫描新分支、Job URL 正确且有访问权限: {job_url}'
        )

    def mode(self, job_url):
        self.visit(job_url)
        # Use actionable links only. Raw page text can contain old log messages
        # like 'Build Now' and must never authorize a build click.
        operation('PW', 'page.get_by_role(link, name=Build with Parameters).count()')
        if self.page.get_by_role('link', name=re.compile(r'Build with Parameters', re.I)).count():
            detected = 'parameters'
        elif self.page.get_by_role('link', name=re.compile(r'Build Now|^Build$', re.I)).count():
            detected = 'build'
        else:
            detected = 'unknown'
        action('JENKINS', f'{getattr(self, "site", "jenkins")} 页面入口：{detected}（{job_url}）')
        return detected

    def wait_parameter_mode(self, job_url, minutes=12):
        end = time.monotonic() + minutes * 60
        start = time.monotonic()
        while time.monotonic() < end:
            mode = self.mode(job_url)
            if mode == 'parameters':
                return
            action('WAIT', f'{self.site} 等待 Build with Parameters 出现，已等待 {int(time.monotonic()-start)}s')
            time.sleep(self.poll)
        raise NeedsAttention('初始化后未出现 Build with Parameters，请检查 Job 和 Jenkinsfile')

    def build_numbers(self, job_url):
        # Must be job-local links: never "last successful" or jobs of other users.
        root = urlsplit(job_url)
        path = root.path.rstrip('/') + '/'
        operation('PW', 'page.locator(a[href]).evaluate_all(hrefs) 读取本 Job 的 Build 链接')
        hrefs = self.page.locator('a[href]').evaluate_all('(els) => els.map(e => e.href)')
        numbers = set()
        for href in hrefs:
            p = urlsplit(href)
            if p.hostname != root.hostname or not p.path.startswith(path):
                continue
            trailing = p.path[len(path):]
            found = re.match(r'^(\d+)(?:/|$)', trailing)
            if found:
                numbers.add(int(found[1]))
        operation('PW', f'筛选出 Build Numbers={sorted(numbers)}')
        return numbers

    def _safe_click(self, text, role):
        operation('PW', f'page.get_by_role({role}, name=^{text}$).count()')
        loc = self.page.get_by_role(role, name=re.compile(r'^' + re.escape(text) + r'$', re.I))
        if loc.count() == 1:
            action('CLICK', f'{self.site}: {role} {text}')
            operation('PW', f'locator.click() site={self.site} role={role} name={text}')
            loc.click()
            debug(f'{self.site} click {role} {text} 完成 -> {self.page.url}')
            return
        if loc.count() > 1:
            raise NeedsAttention(f'Jenkins 出现多个 {text} 控件，不自动点击')
        # Legacy Jenkins buttons use input[type=submit], links may wrap icon spans.
        if role == 'button':
            for css in [f'input[type="submit"][value="{text}"]', f'button:has-text("{text}")']:
                matches = self.page.locator(css)
                if matches.count() == 1:
                    action('CLICK', f'{self.site}: {css}')
                    operation('PW', f'locator({css}).click()')
                    matches.click()
                    debug(f'{self.site} click {css} 完成 -> {self.page.url}')
                    return
        raise NeedsAttention(f'页面找不到唯一的 {role}: {text}，请查看 screenshots')

    def _find_control(self, field, config_css=''):
        if config_css:
            operation('PW', f'page.locator({config_css!r}) 配置的 {field} 字段')
            chosen = self.page.locator(config_css)
            if chosen.count() != 1:
                raise NeedsAttention(f'{field} 配置选择器匹配 {chosen.count()} 个控件（必须为1）')
            return chosen
        # Fields in Jenkins are often not connected to standard <label for>.
        operation('PW', f'page.get_by_label({field}, exact=True).count()')
        by_label = self.page.get_by_label(field, exact=True)
        if by_label.count() == 1:
            return by_label
        operation('PW', f'page.get_by_text({field}, exact=True).count()')
        labels = self.page.get_by_text(field, exact=True)
        if labels.count() < 1:
            raise NeedsAttention(f'未找到 Jenkins 参数 {field}，请配置 jenkins.gmx_fields.{field}')
        for i in range(labels.count()):
            lab = labels.nth(i)
            for depth in range(1, 5):
                parent = lab.locator('xpath=' + '/'.join('..' for _ in range(depth)))
                operation('PW', f'检查 {field} 标签祖先节点 depth={depth} 的控件数量')
                controls = parent.locator('select, input:not([type="hidden"]), textarea')
                if controls.count() == 1:
                    return controls.first
        raise NeedsAttention(f'{field} 有多个或没有可识别控件，请配置 jenkins.gmx_fields.{field}')

    @staticmethod
    def _set_checkbox(control, selected, field):
        """Use Jenkins' own visible label when its checkbox is covered/hidden."""
        if control.get_attribute('type') != 'checkbox' or type(selected) is not bool:
            raise NeedsAttention(f'{field} 不是有效的布尔复选框')
        if control.is_checked() == selected:
            return
        try:
            if selected:
                control.check(timeout=2000)
            else:
                control.uncheck(timeout=2000)
        except PlaywrightTimeout:
            # Do not force-click through arbitrary overlays. Only click a label
            # associated with this exact input, including Jenkins attach-previous.
            if not control.is_enabled():
                raise NeedsAttention(f'{field} 已禁用，禁止继续提交')
            if control.is_checked() == selected:
                return
            handle = control.evaluate_handle('''el => {
                const labels = Array.from(el.labels || []);
                const next = el.nextElementSibling;
                if (next && next.tagName === 'LABEL' &&
                    next.classList.contains('attach-previous') &&
                    (!next.htmlFor || next.htmlFor === el.id)) labels.push(next);
                const visible = [...new Set(labels)].filter(label => {
                    const s = getComputedStyle(label);
                    return label.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
                });
                return visible.length === 1 ? visible[0] : null;
            }''')
            try:
                label = handle.as_element()
                if label is None:
                    raise NeedsAttention(f'{field} 被遮挡且找不到唯一关联标签，禁止继续提交')
                action('FORM', f'{field} 使用该复选框的关联标签设置勾选状态')
                label.click(timeout=3000)
            finally:
                handle.dispose()
        if control.is_checked() != selected:
            raise NeedsAttention(f'{field} 参数回读校验失败，禁止继续提交')

    def fill_gmx_parameters(self, country, project, settings=None):
        settings = settings or {}
        selectors = self.conf.get('gmx_fields', {})
        entity = self._find_control('ENTITIES', selectors.get('ENTITIES', ''))
        tag = entity.evaluate('(el) => el.tagName.toLowerCase()')
        typ = entity.get_attribute('type')
        if tag == 'select':
            options = entity.locator('option').evaluate_all('(es) => es.map(e=>({value:e.value,text:e.textContent.trim()}))')
            exact = [o for o in options if o['value'] == country or o['text'] == country]
            if len(exact) != 1:
                raise NeedsAttention(f'ENTITIES 找不到唯一国家 {country}: {options[:40]}')
            operation('PW', f'ENTITIES.select_option(value={exact[0]["value"]})')
            entity.select_option(value=exact[0]['value'])
        elif tag in ('input', 'textarea') and typ not in ('checkbox', 'radio'):
            operation('PW', f'ENTITIES.fill({country})')
            entity.fill(country)
        else:
            raise NeedsAttention('ENTITIES 不是可识别的下拉框/文本框，请配置字段选择器')
        sync = self._find_control('SYNC_UPLOAD', selectors.get('SYNC_UPLOAD', ''))
        if sync.get_attribute('type') != 'checkbox':
            raise NeedsAttention('SYNC_UPLOAD 不是复选框，请检查 Jenkins 参数')
        should_sync = settings.get('SYNC_UPLOAD', True)
        if not isinstance(should_sync, bool):
            raise NeedsAttention('SYNC_UPLOAD 必须是布尔值')
        operation('PW', f'SYNC_UPLOAD.{"check" if should_sync else "uncheck"}()')
        JenkinsBrowser._set_checkbox(sync, should_sync, 'SYNC_UPLOAD')
        target = settings.get('CUSTOM_FOLDER') or folder(country, project)
        custom = self._find_control('CUSTOM_FOLDER', selectors.get('CUSTOM_FOLDER', ''))
        if custom.evaluate('(el) => el.tagName.toLowerCase()') not in ('input', 'textarea'):
            raise NeedsAttention('CUSTOM_FOLDER 不是文本框')
        operation('PW', f'CUSTOM_FOLDER.fill({target})')
        custom.fill(target)
        # GMX has three additional Jenkins-only flags. Web tasks can set them;
        # CLI tasks without explicit options leave Jenkins defaults unchanged.
        for field in ('SKIP_TEST', 'FORCE', 'KEEP_ALIVE'):
            if field not in settings:
                continue
            selected = settings[field]
            if not isinstance(selected, bool):
                raise NeedsAttention(f'{field} 必须是布尔值')
            control = self._find_control(field, selectors.get(field, ''))
            if control.get_attribute('type') != 'checkbox':
                raise NeedsAttention(f'{field} 不是复选框，拒绝提交 GMX Build')
            operation('PW', f'{field}.{"check" if selected else "uncheck"}()')
            JenkinsBrowser._set_checkbox(control, selected, field)
            if control.is_checked() != selected:
                raise NeedsAttention(f'{field} 参数回读校验失败')
        # CLI tasks that supply no advanced options keep remaining Jenkins defaults.
        # Web tasks provide explicit values and we verify the actual input state.
        for field in ('BUILD_TYPE', 'SIGNING_REPO_BRANCH'):
            if field not in settings:
                continue
            value = settings[field]
            control = self._find_control(field, selectors.get(field, ''))
            tag = control.evaluate('(el) => el.tagName.toLowerCase()')
            if tag == 'select':
                choices = control.locator('option').evaluate_all(
                    '(es) => es.map(e=>({value:e.value,text:e.textContent.trim()}))')
                matched = [c for c in choices if c['value'] == value or c['text'] == value]
                if len(matched) != 1:
                    raise NeedsAttention(f'{field} 无法找到唯一选项 {value}')
                value = matched[0]['value']
                operation('PW', f'{field}.select_option({value})')
                control.select_option(value=value)
            elif tag in ('input', 'textarea') and control.get_attribute('type') not in ('checkbox', 'radio'):
                operation('PW', f'{field}.fill({value})')
                control.fill(value)
            else:
                raise NeedsAttention(f'{field} 输入控件类型不支持')
            if control.input_value() != value:
                raise NeedsAttention(f'{field} 写入后校验失败')
        action('FORM', f'GMX 参数设置 ENTITIES={country} SYNC_UPLOAD={str(should_sync).lower()} CUSTOM_FOLDER={target}')
        operation('PW', '验证 SYNC_UPLOAD.is_checked() && CUSTOM_FOLDER.input_value()')
        if sync.is_checked() != should_sync or custom.input_value() != target:
            raise NeedsAttention('Jenkins 参数填写结果验证失败')
        if entity.input_value().strip() != country:
            raise NeedsAttention('ENTITIES 写入后为空或与选择国家不一致；禁止提交可能被禁用的 GMX Build')
        if 'BUILD_TYPE' in settings and not str(settings['BUILD_TYPE']).strip():
            raise NeedsAttention('BUILD_TYPE 不能为空，否则 GMX Build 会被禁用')
        print(f'[Jenkins/gmx] ENTITIES={country} SYNC_UPLOAD={str(should_sync).lower()} CUSTOM_FOLDER={target}', flush=True)

    def request_build(self, job_url, *, with_parameters, country='', project=''):
        action('JENKINS', f'{self.site} 准备触发 {"Build with Parameters" if with_parameters else "首次 Build"}')
        self.visit(job_url)
        before = self.build_numbers(job_url)
        if with_parameters:
            self._safe_click('Build with Parameters', 'link')
            operation('PW', 'page.locator(form).count() 检查参数页')
            if self.page.locator('form').count() == 0:
                raise NeedsAttention('参数化构建页面没有表单')
            if self.site == 'gmx':
                self.fill_gmx_parameters(country, project, getattr(self, 'gmx_settings', None))
            # plugin: all defaults untouched
            self._safe_click('Build', 'button')
        else:
            # normal Build Now; some theme labels this "Build"
            operation('PW', 'page.get_by_role(link, name=Build Now).count()')
            if self.page.get_by_role('link', name='Build Now').count():
                self._safe_click('Build Now', 'link')
            else:
                self._safe_click('Build', 'link')
        action('JENKINS', f'{self.site} 已点击 Build，等待本次 Build Number (baseline={sorted(before)})')
        return before

    def detect_new_build(self, job_url, baseline, minutes=12):
        deadline = time.monotonic() + minutes * 60
        start = time.monotonic()
        while time.monotonic() < deadline:
            self.visit(job_url)
            candidates = sorted(self.build_numbers(job_url) - set(baseline))
            if len(candidates) == 1:
                action('JENKINS', f'{self.site} 检测到本次 Build #{candidates[0]}: {job_url}{candidates[0]}/')
                return candidates[0]
            if len(candidates) > 1:
                raise NeedsAttention(f'发现多个新 Build {candidates}，无法确认哪个属于本次触发')
            action('WAIT', f'{self.site} 等待新 Build Number，已等待 {int(time.monotonic()-start)}s；baseline={sorted(baseline)}')
            time.sleep(self.poll)
        raise NeedsAttention(f'触发后没有发现 Build Number。可能仍在队列中，请人工确认: {job_url}')

    def _heading_result(self, number):
        """Only the numbered Build header, never old successful jobs in sidebar."""
        selector = (
            '#build-status-icon, #buildStatus, #build-status, .build-status-icon, '
            '#main-panel .jenkins-build-status, #main-panel .page-headline svg, '
            '#main-panel h1 svg, #main-panel .jenkins-build-status svg, '
            '#main-panel .build-status svg, #main-panel .page-headline .svg-icon'
        )
        operation('PW', f'page.locator({selector!r}) 检查 Build #{number} 标题的状态图标')
        icons = self.page.locator(selector)
        for index in range(min(icons.count(), 12)):
            info = icons.nth(index).evaluate(
                '(el) => [el.className?.baseVal || el.className, '
                'el.getAttribute("title"), el.getAttribute("aria-label"), '
                'el.getAttribute("alt"), el.outerHTML.slice(0,1600)].join(" ")')
            result = result_from_status_markup(info)
            if result:
                action('JENKINS', f'{self.site} Build #{number} 页面标题图标确认终态: {result}')
                return result
        return None

    def _history_result(self, job_url, number):
        """Identify only the status icon adjacent to the *exact* numbered build.

        Recent Jenkins themes use a "Builds" sidebar instead of #buildHistory;
        discover target build links by URL, not by a fixed sidebar selector.
        Store concise, credential-free DOM evidence for diagnostics.
        """
        if not hasattr(self.page, 'evaluate'):
            return None
        operation('PW', f'page.evaluate(精确 Build #{number} 链接及其邻近图标)')
        try:
            evidence = self.page.evaluate(r"""({jobUrl, number}) => {
              const base = new URL(jobUrl);
              const jobPath = base.pathname.replace(/\/+$/, '') + '/';
              const buildPath = jobPath + number + '/';
              const allLinks = Array.from(document.querySelectorAll('a[href]'));
              const buildNumber = link => {
                try {
                  const url = new URL(link.href, document.location.href);
                  if (url.origin !== base.origin) return null;
                  const normalized = url.pathname.replace(/\/+$/, '') + '/';
                  if (!normalized.startsWith(jobPath)) return null;
                  const suffix = normalized.slice(jobPath.length);
                  return /^\d+\/$/.test(suffix) ? Number(suffix.slice(0, -1)) : null;
                } catch { return null; }
              };
              const matches = allLinks.filter(a => buildNumber(a) === Number(number));
              const records = [];
              for (const link of matches.slice(0, 8)) {
                let node = link;
                for (let depth = 0; depth < 8 && node && node !== document.body; depth++, node = node.parentElement) {
                  // NEVER climb into a container containing other build numbers.
                  const numbers = Array.from(node.querySelectorAll('a[href]')).map(buildNumber).filter(v => v !== null);
                  if (numbers.some(v => v !== Number(number))) break;
                  const icons = Array.from(node.querySelectorAll('svg, img, [role="img"]'));
                  if (node.matches?.('svg, img, [role="img"]')) icons.unshift(node);
                  if (!icons.length) continue;
                  const details = icons.slice(0, 12).map(icon => {
                    const rect = icon.getBoundingClientRect();
                    const parts = [icon, ...Array.from(icon.querySelectorAll('use, path, circle, polyline')).slice(0, 8)];
                    const colors = [];
                    for (const part of parts) {
                      const css = getComputedStyle(part);
                      for (const key of ['color','fill','stroke']) {
                        const val = css[key];
                        if (val && !colors.includes(val)) colors.push(val);
                      }
                    }
                    const style = getComputedStyle(icon);
                    const meaningful = icon.tagName.toLowerCase() === 'svg' || icon.tagName.toLowerCase() === 'img';
                    return {
                      markup: icon.outerHTML.slice(0, 2200),
                      tag: icon.tagName.toLowerCase(),
                      colors: colors.slice(0, 12),
                      visible: style.display !== 'none' && style.visibility !== 'hidden' &&
                        rect.width > 0 && rect.height > 0,
                      width: Math.round(rect.width), height: Math.round(rect.height),
                      visualIcon: meaningful && rect.width <= 80 && rect.height <= 80
                    };
                  });
                  records.push({depth, container: node.tagName.toLowerCase(), icons: details});
                  break; // the closest candidate containing an icon is the safest
                }
              }
              return {targetLinks: matches.length, candidates: records,
                      pageBuildLinks: allLinks.filter(a => buildNumber(a) !== null).length};
            }""", {'jobUrl': job_url, 'number': number})
        except PlaywrightError as exc:
            setattr(self, 'last_history_probe', {'error': type(exc).__name__})
            debug(f'{self.site} Build #{number} Build History DOM 无法读取: {type(exc).__name__}')
            return None
        # Stubs used by older tests may return a markup string rather than a structured probe.
        if isinstance(evidence, str):
            return result_from_status_markup(evidence)
        if not isinstance(evidence, dict):
            return None
        setattr(self, 'last_history_probe', evidence)
        results = set()
        source = []
        for row in evidence.get('candidates', []):
            for icon in row.get('icons', []):
                outcome = result_from_status_markup(icon.get('markup', ''))
                how = 'semantic'
                if not outcome and icon.get('visualIcon') and icon.get('visible'):
                    # Only small, visible icon graphics in the row of precisely
                    # this Build. A green stage bar or old build icon is excluded.
                    outcome = status_from_icon_colors(icon.get('colors', []))
                    how = 'icon-color'
                if outcome:
                    results.add(outcome)
                    source.append(f'{how}:{outcome}')
        evidence['recognized'] = source[:8]
        if len(results) == 1:
            status = next(iter(results))
            action('JENKINS', f'{self.site} Build History #{number} 专属图标确认终态: {status}')
            return status
        if len(results) > 1:
            action('WAIT', f'{self.site} Build History #{number} 多个图标状态冲突: {sorted(results)}')
        return None

    def _fast_console_result(self, build_url, number):
        """Read raw consoleText HTTP response through this browser's cookie context.

        No Jenkins REST API, no browser DOM rendering, no public credentials.
        A 200 response can still be an INCOMPLETE running log: accept only the
        final Finished marker. Never take an earlier SUCCESS in log history.
        """
        url = build_url + 'consoleText'
        operation('PW', f'context.request.get({url}, timeout=10000) [原始日志，非页面渲染]')
        try:
            response = authenticated_console_response(self, url, 10000)
            status = response.status
            final_host = urlsplit(response.url).hostname
            if (final_host != urlsplit(url).hostname
                    or urlsplit(response.url).path != urlsplit(url).path):
                action('WAIT', f'{self.site} Build #{number} 原始日志请求发生登录跳转，检查浏览器会话')
                return None
            if status in (401, 403):
                raise NeedsAttention(f'{self.site} Build #{number} 原始日志 HTTP {status}，请检查 Jenkins 权限/SSO')
            if status != 200:
                action('WAIT', f'{self.site} Build #{number} 原始日志 HTTP {status}，使用页面状态备用检查')
                return None
            # HTTP response contains the server text directly, even when
            # Chrome Console Output is still busy asynchronously rendering.
            tail = response.text()[-30000:]
            result = final_result_from_console(tail)
            if result:
                action('JENKINS', f'{self.site} Build #{number} 原始 Console 已确认: {result}（无需等待页面加载）')
                return result, tail
            return None
        except (PlaywrightTimeout, PlaywrightError) as exc:
            action('WAIT', f'{self.site} Build #{number} 原始日志暂不可读 ({type(exc).__name__})，切换备用检测')
            return None

    def _wait_result_fast(self, job_url, number, minutes=None, *, watch_callback=False):
        """5s default result detection; slower header/callback probe as fallback."""
        deadline = time.monotonic() + 60 * float(minutes or self.conf.get('build_timeout_minutes', 180))
        start = time.monotonic()
        build_url = job_url + str(number) + '/'
        interval = max(2, min(60, int(getattr(self, 'result_poll', 5))))
        action('JENKINS', f'{self.site} Build #{number}：优先检查绿色状态勾（每 {interval}s），'
                         '日志仅作补充确认')
        while time.monotonic() < deadline:
            # The job's Build History is lightweight and contains the green tick
            # visible next to this EXACT Build number. Probe it BEFORE loading
            # the often huge consoleText, which can take >10s to download.
            code = self.visit(job_url)
            if code in (401, 403):
                raise NeedsAttention(f'{self.site} Job 页面无访问权限 (HTTP {code})')
            if code == 200:
                result = JenkinsBrowser._history_result(self, job_url, number)
                if result:
                    return result, ''
            code = self.visit(build_url)
            if code in (401, 403):
                raise NeedsAttention(f'{self.site} Build #{number} 页面无访问权限 (HTTP {code})')
            if code == 200:
                result = JenkinsBrowser._heading_result(self, number)
                if result:
                    return result, ''
                if watch_callback:
                    child = self.find_callback(job_url, number)
                    if child is not None:
                        action('CALLBACK', f'{self.site} Build #{number} 已创建 Callback #{child}；改为监控 #{child}')
                        return 'DELEGATED', str(child)
            # If the icons are not exposed by this Jenkins theme, fall back to
            # the explicit final marker (never an arbitrary SUCCESS in log text).
            found = JenkinsBrowser._fast_console_result(self, build_url, number)
            if found:
                return found
            action('WAIT', f'{self.site} Build #{number} 仍未确认终态；已等待 '
                           f'{int(time.monotonic()-start)}s，{interval}s 后检查状态勾')
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise TimeoutError(f'{self.site} Build #{number} 结果未确认，超出构建监控时间；'
                           f'请直接检查 {build_url}consoleText')

    def wait_result(self, job_url, number, minutes=None, *, watch_callback=False):
        # Ordinary real browser contexts always have context.request. Old unit
        # stubs can still exercise the conservative DOM-based fallback below.
        if getattr(getattr(self, 'context', None), 'request', None) is not None:
            return JenkinsBrowser._wait_result_fast(self, job_url, number, minutes,
                                                    watch_callback=watch_callback)
        deadline = time.monotonic() + 60 * float(minutes or self.conf.get('build_timeout_minutes', 180))
        start = time.monotonic()
        action('JENKINS', f'{self.site} 正在监控 Build #{number}: {job_url}{number}/')
        build_url = job_url + str(number) + '/'
        while time.monotonic() < deadline:
            # Legacy fallback if a browser request context is not available.
            code = self.visit(build_url + 'consoleText', timeout=120000)
            if code == 200:
                remaining = max(0, deadline - time.monotonic())
                settle = min(max(1, float(self.conf.get('console_settle_seconds', 10))), remaining)
                if settle > 0 and hasattr(self.page, 'wait_for_function'):
                    operation('PW', f'page.wait_for_function(Finished marker at END of console, timeout={settle:g}s)')
                    try:
                        self.page.wait_for_function(
                            r"""() => {
                              const t = document.body?.innerText || document.body?.textContent || '';
                              const last = t.trim().split(/\r?\n/).filter(s => s.trim()).pop() || '';
                              return /^Finished:\s*(SUCCESS|FAILURE|ABORTED|UNSTABLE|NOT_BUILT)\s*$/i.test(last);
                            }""",
                            timeout=int(settle * 1000), polling=500)
                    except PlaywrightTimeout:
                        action('WAIT', f'{self.site} Build #{number} Console Output 尚未到 Finished，改查 Build 状态')
                operation('PW', 'page.locator(body).evaluate(后30000字符) [日志正文不直接打印]')
                end = self.page.locator('body').evaluate('(e) => e.innerText.slice(-30000)')
                result = final_result_from_console(end)
                if result:
                    action('JENKINS', f'{self.site} Build #{number} Console 最后一行确认: {result}')
                    return result, end
            elif code in (401, 403):
                raise NeedsAttention(f'{self.site} Build #{number} Console 无权读取 (HTTP {code})')
            code = self.visit(build_url)
            if code not in (200,):
                raise NeedsAttention(f'Build #{number} 页面 HTTP {code}')
            result = JenkinsBrowser._heading_result(self, number)
            if result:
                return result, ''
            if watch_callback:
                child = self.find_callback(job_url, number)
                if child is not None:
                    action('CALLBACK', f'{self.site} Build #{number} 已创建关联 Callback #{child}；切换监控 #{child}')
                    return 'DELEGATED', str(child)
            action('WAIT', f'{self.site} Build #{number} 状态尚未确认，已等待 {int(time.monotonic()-start)}s')
            time.sleep(min(self.poll, max(0, deadline - time.monotonic())))
        raise TimeoutError(f'Jenkins Build #{number} 在监控超时后仍未确认: {build_url}')

    def find_callback(self, job_url, source_number):
        self.visit(job_url)
        operation('PW', 'page.locator(body).inner_text() 查找本次关联的 Callback')
        text = self.page.locator('body').inner_text()
        return callback_number_from_text(text, source_number)


def callback_number_from_text(text, source_number):
    """Read history cards, checking the explicit 'Manual Callback ← Source #n'."""
    blocks = re.split(r'(?m)(?=^[ \t]*#\d+(?:\s|$))', text)
    candidates = set()
    for block in blocks:
        match = re.match(r'^[ \t]*#(\d+)\b', block)
        if not match or 'Manual Callback' not in block:
            continue
        number = int(match[1])
        if number <= source_number:
            continue
        if re.search(r'\bSource\s*#' + str(source_number) + r'\b', block, re.I):
            candidates.add(number)
    if len(candidates) > 1:
        raise NeedsAttention(f'多个 Callback 指向 Source #{source_number}: {sorted(candidates)}')
    return next(iter(candidates)) if candidates else None



def ensure_bootstrap(browser: JenkinsBrowser, job_url: str, task_id: str, site: str):
    """First plain Build may fail while Jenkins loads parameter definitions."""
    rt = read_state(task_id)['runtime']
    key = site + '_bootstrap'
    if rt.get(key + '_completed'):
        return
    browser.wait_job(job_url)
    mode = browser.mode(job_url)
    if mode == 'parameters':
        action('JENKINS', f'{site} 已具有参数化入口，跳过初始化 Build')
        patch_runtime(task_id, **{key + '_completed': True})
        return
    if mode != 'build':
        raise NeedsAttention(f'{site} Jenkins 未显示 Build 或 Build with Parameters')
    num = rt.get(key + '_number')
    if not num:
        if key + '_baseline' in rt:
            # After an interruption we do not click again; avoid duplicate runs.
            num = browser.detect_new_build(job_url, rt[key + '_baseline'], minutes=3)
        else:
            # NOTE: transient interruption between click and persisted baseline can be
            # recovered by inspecting new build history; do not click twice.
            browser.visit(job_url)
            baseline = sorted(browser.build_numbers(job_url))
            action('JENKINS', f'{site} 初始化前已有 Build: {baseline}')
            patch_runtime(task_id, **{key + '_baseline': baseline})
            browser.request_build(job_url, with_parameters=False)
            num = browser.detect_new_build(job_url, baseline)
        patch_runtime(task_id, **{key + '_number': num})
    if site == 'gmx':
        # GMX's first unparameterized branch-indexing build can be DISABLED
        # ("ENTITIES or BUILD_TYPE is empty"). It is only an initialization
        # attempt, not the real app build. Wait for the parameter form instead
        # of polling the disabled #1 for hours or requiring it to succeed.
        action('JENKINS', f'GMX 初始化 Build #{num}：只等待参数化入口，不将其结果当正式构建结果')
        browser.wait_parameter_mode(job_url)
        patch_runtime(task_id, **{key + '_completed': True,
                                  key + '_result': 'PARAMETER_FORM_READY'})
        return
    if not rt.get(key + '_result'):
        result, _ = browser.wait_result(job_url, num)
        patch_runtime(task_id, **{key + '_result': result})
        print(f'[{site}] 初始化构建 #{num}: {result}；等待参数化入口', flush=True)
    browser.wait_parameter_mode(job_url)
    patch_runtime(task_id, **{key + '_completed': True})


def formal_build(browser: JenkinsBrowser, job_url, task_id, site, country='', project=''):
    rt = read_state(task_id)['runtime']
    key = site + '_formal'
    num = rt.get(key + '_number')
    if not num:
        if key + '_baseline' in rt:
            num = browser.detect_new_build(job_url, rt[key + '_baseline'], minutes=3)
        else:
            browser.visit(job_url)
            baseline = sorted(browser.build_numbers(job_url))
            action('JENKINS', f'{site} 正式构建前已有 Build: {baseline}')
            patch_runtime(task_id, **{key + '_baseline': baseline})
            browser.request_build(job_url, with_parameters=True, country=country, project=project)
            num = browser.detect_new_build(job_url, baseline)
        patch_runtime(task_id, **{key + '_number': num})
    result = rt.get(key + '_result')
    console = ''
    if not result:
        if site == 'plugin':
            result, console = browser.wait_result(job_url, num, watch_callback=True)
            if result == 'DELEGATED':
                child = int(console)
                if child <= num:
                    raise NeedsAttention(f'Callback #{child} 不在 Plugin Trigger #{num} 之后')
                patch_runtime(task_id, plugin_callback_number=child)
                action('CALLBACK', f'Plugin Trigger #{num} → Callback #{child}；下一步监控 Callback')
                console = ''
        else:
            action('JENKINS', f'GMX 正式参数化 Build #{num}：直接监控当前编号（不采用 Plugin 的 Callback 转交模型）')
            result, console = browser.wait_result(job_url, num)
        patch_runtime(task_id, **{key + '_result': result})
    if result == 'DELEGATED' and site == 'plugin':
        if not read_state(task_id)['runtime'].get('plugin_callback_number'):
            child = browser.find_callback(job_url, num)
            if child is None:
                raise NeedsAttention(f'Plugin Trigger #{num} 记录了关联构建，但恢复时找不到对应 Callback')
            patch_runtime(task_id, plugin_callback_number=child)
    if result != 'SUCCESS' and not (site == 'plugin' and result == 'DELEGATED'):
        raise RuntimeError(f'{site} 正式构建 #{num} 失败: {result} ({job_url}{num}/)')
    return num, console


def authenticated_console_response(browser, url, timeout):
    """Read-only retry after normal browser login; never replay a build POST."""
    response = browser.context.request.get(url, timeout=timeout)
    headers = getattr(response, 'headers', {})
    headers = headers if isinstance(headers, dict) else {}
    needs_auth = (response.status in (401, 403) or urlsplit(response.url) != urlsplit(url)
                  or 'html' in headers.get('content-type', '').lower())
    visit = getattr(browser, 'visit', None)
    if needs_auth and callable(visit):
        # visit owns the bounded, site-specific login and explicit ACL guard.
        if hasattr(response, 'dispose'):
            response.dispose()
        if visit(url, timeout=timeout) != 200:
            raise NeedsAttention('指定 Build 日志认证恢复后仍不可访问')
        response = browser.context.request.get(url, timeout=timeout)
    return response


def read_full_console(browser, job_url, number, emit=None):
    """Download the whole numbered build log through its authenticated session."""
    url = job_url + str(number) + '/consoleText'
    def report(message):
        if emit is None:
            action('CONSOLE', message)
        else:
            emit(message)
    report(f'[DOWNLOAD] 完整日志请求：{url}；使用浏览器登录会话，不截取日志尾部')
    timeout = min(1800, max(30, float(getattr(browser, 'conf', {}).get('console_download_timeout_seconds', 300))))
    report(f'[DOWNLOAD] 下载超时={timeout:g}s；请求完整日志，页面只显示提取摘要')
    response = authenticated_console_response(browser, url, int(timeout * 1000))
    headers = getattr(response, 'headers', {})
    content_type = headers.get('content-type', 'unknown')
    report(f'[HTTP] consoleText={response.status}; type={content_type}; response URL={response.url}')
    if response.status != 200 or urlsplit(response.url) != urlsplit(url):
        raise NeedsAttention('指定 Build 完整日志不可读或发生登录跳转')
    console = response.text()
    byte_count = sum(len(console[i:i + 1048576].encode('utf-8')) for i in range(0, len(console), 1048576))
    line_count = console.count('\n') + int(bool(console) and not console.endswith('\n'))
    report(f'[DOWNLOAD] 完整响应已读取：bytes={byte_count}; lines={line_count}')
    if hasattr(response, 'dispose'):
        response.dispose()
    if 'html' in content_type.lower() or re.match(r'\s*<(?:!doctype\s+html|html)\b', console, re.I):
        raise NeedsAttention('consoleText 返回 HTML，尚未取得原始构建日志')
    if headers.get('x-more-data', '').lower() == 'true':
        raise NeedsAttention('服务器返回未完成的分段日志，禁止使用其中的制品数据')
    if not console or console.isspace():
        raise NeedsAttention('指定 Build 日志为空')
    return console


def plugin_upload_hashes(browser, job_url, number, version, *, confirmed_success=False):
    from .nexus import parse_upload_metadata
    console = read_full_console(browser, job_url, number)
    result = final_result_from_console(console)
    if result and result != 'SUCCESS':
        raise NeedsAttention(f'Plugin 日志终态={result}，与成功构建冲突')
    if not confirmed_success and result != 'SUCCESS':
        raise NeedsAttention('Plugin 构建成功尚未独立确认，禁止使用其中的 SHA256')
    try:
        metadata = parse_upload_metadata(
            console, emit=lambda message: action('NEXUS', message))
        if metadata['version'] != version:
            raise ValueError('Nexus 上传版本与当前两个 Artifacts 版本不同')
        return metadata['hashes']
    except ValueError as exc:
        raise NeedsAttention(str(exc)) from exc


def _plugin_upload_from_build(browser, job_url, number):
    """Read ONE numbered build's full log and return (version, hashes).

    The version and SHA256 both come from the same Nexus upload output JSON, so
    the Jenkins Artifact browser (and its slow zip viewer links) is never opened.
    Raises ValueError when this build's log has no usable Nexus upload output.
    """
    from .nexus import parse_upload_metadata
    console = read_full_console(browser, job_url, number)
    result = final_result_from_console(console)
    metadata = parse_upload_metadata(console, emit=lambda message: action('NEXUS', message))
    if result and result != 'SUCCESS':
        raise NeedsAttention(f'Plugin Build #{number} 日志终态={result}，与成功构建冲突')
    return metadata['version'], metadata['hashes']


def plugin_artifact_info(browser, job_url, source_number, task_id):
    """Return (version, hashes, build_number) from the Plugin build's own log.

    The source build is tried first; when it only triggered a downstream
    Callback build, the Callback's log is used instead.
    """
    key = 'plugin_callback_number'
    rt = read_state(task_id)['runtime']

    def from_callback(child):
        result, _ = browser.wait_result(job_url, child)
        if result != 'SUCCESS':
            raise RuntimeError(f'Plugin Callback #{child} 失败: {result}')
        try:
            version, hashes = _plugin_upload_from_build(browser, job_url, child)
        except ValueError as exc:
            raise NeedsAttention(f'Plugin Callback #{child} 日志中没有可用的 Nexus 上传记录: {exc}') from exc
        return version, hashes, child

    known = rt.get(key)
    if known:
        return from_callback(known)
    try:
        version, hashes = _plugin_upload_from_build(browser, job_url, source_number)
        return version, hashes, source_number
    except ValueError:
        pass
    deadline = time.monotonic() + 60 * float(browser.conf.get('callback_timeout_minutes', 30))
    while time.monotonic() < deadline:
        child = browser.find_callback(job_url, source_number)
        if child is not None:
            patch_runtime(task_id, **{key: child})
            return from_callback(child)
        time.sleep(browser.poll)
    raise NeedsAttention('正式 Plugin Build 已成功，但在对应 Build 或 Callback 的日志中没有找到 Nexus 上传记录。'
                         '请检查 Jenkins 页面小箭头的具体结构与下游 Job。')


def plugin_artifact_version(browser, job_url, source_number, task_id):
    """Backward-compatible wrapper: (version, build_number)."""
    version, _hashes, number = plugin_artifact_info(browser, job_url, source_number, task_id)
    return version, number
