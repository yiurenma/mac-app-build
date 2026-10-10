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
from urllib.parse import urlsplit, unquote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError
from .auth import read_credentials, approved_login_host, submit_recognized_login
from .versions import parse_artifacts, folder
from .store import patch_runtime, read_state
from .telemetry import action, debug, operation


class NeedsAttention(RuntimeError):
    pass


def final_result_from_console(text):
    """Recognize only the final Jenkins console status line, not quoted log text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    match = re.fullmatch(r'Finished:\s*(SUCCESS|FAILURE|ABORTED|UNSTABLE|NOT_BUILT)',
                         lines[-1], re.I)
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


def interactive_login(config, data_dir: Path, site: str):
    parent = config['jenkins'][site + '_parent'].rstrip('/').removesuffix('/job') + '/'
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
    def __init__(self, config, data_dir: Path, task_dir: Path, site: str, *, visible=False, verbose=False):
        self.conf = config['jenkins']
        self.site = site
        self.visible = bool(visible)
        self.verbose = bool(verbose)
        self.task_dir = task_dir
        self.screens = task_dir / 'screenshots'
        self.screens.mkdir(parents=True, exist_ok=True)
        auth = saved_auth_path(data_dir, site)
        self.auth_path = auth
        self.credentials = read_credentials()
        if not auth.exists() and not self.credentials:
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

    def _login_if_needed(self, expected_url):
        host = urlsplit(expected_url).hostname
        redirected = urlsplit(self.page.url).hostname != host
        password_page = self.page.locator('input[type="password"]').count() > 0
        login_input = (self.page.locator('input[name="j_username"]').count() > 0
                       or self.page.locator('input[name="loginfmt"]').count() > 0)
        if not (redirected or password_page or login_input):
            return False
        if not self.credentials:
            raise NeedsAttention(f'{self.site} 会话已过期且未配置 Keychain 认证；'
                                 '请用 --jenkins-user / --jenkins-password 初始化')
        extra = self.conf.get('auth_allowed_hosts', ['*.hsbc'])
        if not approved_login_host(self.page.url, expected_url, extra):
            raise NeedsAttention(f'{self.site} 登录跳转到未获批准域名：'
                                 f'{urlsplit(self.page.url).hostname}。'
                                 '请先核实域名，再加入 config.yaml 的 jenkins.auth_allowed_hosts')
        username, password = self.credentials
        action('AUTH', f'{self.site} 检测到登录页面，尝试自动填写账号密码')
        if not submit_recognized_login(self.page, username, password,
                                       log=lambda m: operation('PW', m)):
            raise NeedsAttention(f'{self.site} 登录页控件无法安全识别；'
                                 '请检查公司 SSO 页面或改用 --live 人工处理')
        try:
            self.page.wait_for_url(lambda u: urlsplit(str(u)).hostname == host,
                                   timeout=25000)
        except PlaywrightTimeout:
            if not self.visible:
                raise NeedsAttention(f'{self.site} SSO 登录后未返回 Jenkins，'
                                     '可能需要 MFA；请用 --live 运行以查看认证过程')
            action('AUTH', f'{self.site} 等待 SSO/MFA 完成，最多 90 秒')
            try:
                self.page.wait_for_url(lambda u: urlsplit(str(u)).hostname == host,
                                       timeout=90000)
            except PlaywrightTimeout as e:
                raise NeedsAttention(f'{self.site} SSO/MFA 未完成，需要人工检查') from e
        # Verify no password form remained; only then save state.
        if self.page.locator('input[type="password"]').count():
            raise NeedsAttention(f'{self.site} 仍停留在认证页面，可能是密码错误或额外验证')
        self._cache_login()
        return True

    def visit(self, url, timeout=60000):
        operation('PW', f'page.goto(url={url}, wait_until=domcontentloaded, timeout={timeout})')
        started = time.monotonic()
        debug(f'[BROWSER/{self.site}] navigate {url}')
        try:
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        except PlaywrightTimeout as e:
            raise NeedsAttention(f'Jenkins 页面访问超时: {url}') from e
        # Reuse saved SSO state first; log in again only when it expired.
        authenticated = self._login_if_needed(url)
        if authenticated:
            operation('PW', f'page.goto({url}) 登录后重新加载目标 Job')
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            # Do not keep retrying invalid credentials.
            if urlsplit(self.page.url).hostname != urlsplit(url).hostname or \
                    self.page.locator('input[type="password"]').count():
                raise NeedsAttention(f'{self.site} 认证后仍未返回 Jenkins Job 页面')
        code = response.status if response else None
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
            if http == 200 and '404 Not Found' not in self.page.title():
                print(f'[Jenkins/{self.site}] Job 已发现: {job_url}（检查 {attempts} 次）', flush=True)
                return
            if http in (401, 403):
                raise NeedsAttention(f'无权查看 Jenkins Job (HTTP {http}): {job_url}')
            # A 404 is expected while branch indexing has not created the job.
            # Retry only until the configured discovery timeout and never click
            # Build while Jenkins reports Not Found.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(self.discover_poll, remaining)
            action('WAIT', f'{self.site} Job 暂未就绪：HTTP {http}，累计等待 {int(time.monotonic()-start)}s，{delay:g}s 后重试（第 {attempts} 次检查）')
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
        if should_sync:
            sync.check()
        else:
            sync.uncheck()
        target = settings.get('CUSTOM_FOLDER') or folder(country, project)
        custom = self._find_control('CUSTOM_FOLDER', selectors.get('CUSTOM_FOLDER', ''))
        if custom.evaluate('(el) => el.tagName.toLowerCase()') not in ('input', 'textarea'):
            raise NeedsAttention('CUSTOM_FOLDER 不是文本框')
        operation('PW', f'CUSTOM_FOLDER.fill({target})')
        custom.fill(target)
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
        """Read the status icon attached to precisely Build #number in Build History.

        Called ONLY while viewing this job's main page. The page may show many
        green ticks for earlier runs: never inspect a whole sidebar or accept
        another build's icon.
        """
        if not hasattr(self.page, 'evaluate'):
            return None
        operation('PW', f'page.evaluate(Build History #{number} exact-link status icon)')
        try:
            markup = self.page.evaluate(r"""({jobUrl, number}) => {
              const base = new URL(jobUrl);
              const target = base.pathname.replace(/\/+$/, '') + '/' + number + '/';
              const links = document.querySelectorAll(`
                #buildHistory a[href], [id*="buildHistory"] a[href],
                .build-history a[href], [class*="build-history"] a[href],
                .jenkins-builds-list a[href], [class*="builds-list"] a[href],
                [data-build-number] a[href], .build-row a[href]`);
              for (const link of links) {
                let url;
                try { url = new URL(link.href, document.location.href); }
                catch { continue; }
                if (url.host !== base.host || url.pathname.replace(/\/+$/, '') + '/' !== target) continue;
                const row = link.closest(`
                  [data-build-number], .build-row, .build-history-entry,
                  .jenkins-builds-list__item, .jenkins-build-history__item, li, tr`);
                if (!row) continue;
                const rowNumber = row.getAttribute('data-build-number');
                if (rowNumber && rowNumber !== String(number)) continue;
                // Prevent accidentally using an ancestor with icons from many builds.
                const other = Array.from(row.querySelectorAll('a[href]')).some(a => {
                  try {
                    const u = new URL(a.href, document.location.href);
                    const m = u.pathname.match(/\/(\d+)\/$/);
                    return u.host === base.host && m && Number(m[1]) !== Number(number)
                      && u.pathname.startsWith(base.pathname);
                  } catch { return false; }
                });
                if (other) continue;
                const icons = row.querySelectorAll(`
                  .build-status-icon, [class*="build-status"], [class*="status-icon"],
                  svg[title], svg[aria-label], img[alt], svg use, svg, img`);
                // Read status icon markup only; never infer success from a green
                // progress bar, navigation label or unrelated build's status.
                const matches = Array.from(icons).map(el => el.outerHTML).slice(0, 12);
                return matches.join(' ');
              }
              return null;
            }""", {'jobUrl': job_url, 'number': number})
        except PlaywrightError as exc:
            debug(f'{self.site} Build #{number} Build History DOM 无法读取: {type(exc).__name__}')
            return None
        result = result_from_status_markup(markup) if markup else None
        if result:
            action('JENKINS', f'{self.site} Build History #{number} 专属图标确认终态: {result}')
        return result

    def _fast_console_result(self, build_url, number):
        """Read raw consoleText HTTP response through this browser's cookie context.

        No Jenkins REST API, no browser DOM rendering, no public credentials.
        A 200 response can still be an INCOMPLETE running log: accept only the
        final Finished marker. Never take an earlier SUCCESS in log history.
        """
        url = build_url + 'consoleText'
        operation('PW', f'context.request.get({url}, timeout=10000) [原始日志，非页面渲染]')
        try:
            response = self.context.request.get(url, timeout=10000)
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

    def build_artifact_paths(self, job_url, number):
        """Traverse THIS numbered Build's Artifact browser (not Last Successful).

        Jenkins often shows folders instead of linking tarballs on the initial
        artifact page. Limit crawl to one numbered build, 40 pages, and never
        execute/download artifact contents.
        """
        base = job_url + str(number) + '/artifact/'
        base_url = urlsplit(base)
        queue = [base]
        visited = set()
        paths = set()
        action('ARTIFACT', f'{getattr(self, "site", "jenkins")} 读取 Build #{number} 的产物目录')
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            if len(visited) >= 40:
                raise NeedsAttention('Jenkins Artifact 目录过多，已停止自动遍历')
            visited.add(url)
            if self.visit(url) != 200:
                continue
            operation('PW', f'page.locator(a[href]).evaluate_all(hrefs) 读取 Artifact 路径 {url}')
            hrefs = self.page.locator('a[href]').evaluate_all('(els) => els.map(a => a.href)')
            for href in hrefs:
                item = urlsplit(href)
                if ((item.scheme, item.netloc) != (base_url.scheme, base_url.netloc)
                        or not item.path.startswith(base_url.path)):
                    continue
                tail = unquote(item.path[len(base_url.path):])
                if not tail or any(x in ('.', '..') for x in tail.split('/')):
                    continue
                if item.path.endswith('/'):
                    new_url = item._replace(query='', fragment='').geturl()
                    if new_url not in visited and new_url not in queue:
                        queue.append(new_url)
                else:
                    paths.add('build-artifact/' + tail)
        operation('PW', f'本次 Build Artifact 文件数={len(paths)}，候选 tar.gz={[x for x in sorted(paths) if x.endswith(".tar.gz")]}')
        return sorted(paths)

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


def plugin_artifact_version(browser, job_url, source_number, task_id):
    key = 'plugin_callback_number'
    rt = read_state(task_id)['runtime']
    known = rt.get(key)
    if known:
        result, _ = browser.wait_result(job_url, known)
        if result != 'SUCCESS':
            raise RuntimeError(f'Plugin Callback #{known} 失败: {result}')
        return parse_artifacts(browser.build_artifact_paths(job_url, known)), known
    paths = browser.build_artifact_paths(job_url, source_number)
    try:
        return parse_artifacts(paths), source_number
    except ValueError:
        pass
    deadline = time.monotonic() + 60 * float(browser.conf.get('callback_timeout_minutes', 30))
    while time.monotonic() < deadline:
        child = browser.find_callback(job_url, source_number)
        if child is not None:
            patch_runtime(task_id, **{key: child})
            result, _ = browser.wait_result(job_url, child)
            if result != 'SUCCESS':
                raise RuntimeError(f'Plugin Callback #{child} 失败: {result}')
            return parse_artifacts(browser.build_artifact_paths(job_url, child)), child
        time.sleep(browser.poll)
    raise NeedsAttention('正式 Plugin Build 已成功，但在对应 Build 或 Callback 中没有找到两个 Artifacts。'
                         '请检查 Jenkins 页面小箭头的具体结构与下游 Job。')
