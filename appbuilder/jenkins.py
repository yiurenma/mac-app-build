"""Jenkins automation through visible browser pages (NO Jenkins REST API).

Real corporate SSO and customized Jenkins DOM can differ. Unrecognized screens
raise NeedsAttention rather than clicking a potentially incorrect control.
"""
from __future__ import annotations
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
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
    if re.search(r'icon-(?:running|progress|building|pending|queued|anime)\b|\b(?:building|running|progress)\b', value):
        return None
    for outcome, signature in (
        ('FAILURE', r'icon-(?:failure|red)\b|build-status-(?:failure|red)\b|(?:title|aria-label|alt)=["\']failure["\']'),
        ('ABORTED', r'icon-aborted\b|build-status-aborted\b|(?:title|aria-label|alt)=["\']aborted["\']'),
        ('UNSTABLE', r'icon-unstable\b|build-status-unstable\b|(?:title|aria-label|alt)=["\']unstable["\']'),
        ('SUCCESS', r'icon-(?:success|green|blue)\b|build-status-(?:success|green|blue)\b|(?:title|aria-label|alt)=["\']success["\']'),
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
        if not auth.exists():
            raise NeedsAttention(f'未发现 {site} Jenkins 登录态，请先运行: ./app-build login --site {site}')
        self.playwright = sync_playwright().start()
        launch_opts = browser_launch_options(config, headless=not self.visible)
        if self.visible:
            launch_opts['slow_mo'] = 300  # make browser clicks visibly observable, not a stealth run
        operation('PW', f'chromium.launch(channel={launch_opts.get("channel", "chromium")}, headless={launch_opts["headless"]}, slow_mo={launch_opts.get("slow_mo", 0)})')
        self.browser = self.playwright.chromium.launch(**launch_opts)
        action('BROWSER', f'{site} Chrome 已启动（{"可见窗口" if self.visible else "后台模式"}）')
        operation('PW', f'browser.new_context(storage_state=<本地认证文件>, accept_downloads=False)')
        self.context = self.browser.new_context(storage_state=str(auth), accept_downloads=False)
        operation('PW', 'context.new_page()')
        self.page = self.context.new_page()
        self.page.set_default_timeout(12000)
        self.poll = max(10, int(self.conf.get('poll_seconds', 45)))
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

    def visit(self, url, timeout=60000):
        operation('PW', f'page.goto(url={url}, wait_until=domcontentloaded, timeout={timeout})')
        started = time.monotonic()
        debug(f'[BROWSER/{self.site}] navigate {url}')
        try:
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        except PlaywrightTimeout as e:
            raise NeedsAttention(f'Jenkins 页面访问超时: {url}') from e
        # Authentication redirects often move away from Jenkins host.
        expected_host = urlsplit(url).hostname
        actual_host = urlsplit(self.page.url).hostname
        if actual_host != expected_host:
            raise NeedsAttention(f'{self.site} 登录可能过期或跳转到 SSO，请重新运行 login --site {self.site}')
        if self.page.locator('input[type="password"]').count():
            raise NeedsAttention(f'{self.site} 登录已过期，请重新运行 login --site {self.site}')
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

    def fill_gmx_parameters(self, country, project):
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
        operation('PW', 'SYNC_UPLOAD.check()')
        sync.check()
        target = folder(country, project)
        custom = self._find_control('CUSTOM_FOLDER', selectors.get('CUSTOM_FOLDER', ''))
        if custom.evaluate('(el) => el.tagName.toLowerCase()') not in ('input', 'textarea'):
            raise NeedsAttention('CUSTOM_FOLDER 不是文本框')
        operation('PW', f'CUSTOM_FOLDER.fill({target})')
        custom.fill(target)
        action('FORM', f'GMX 参数设置 ENTITIES={country} SYNC_UPLOAD=true CUSTOM_FOLDER={target}')
        operation('PW', '验证 SYNC_UPLOAD.is_checked() && CUSTOM_FOLDER.input_value()')
        if not sync.is_checked() or custom.input_value() != target:
            raise NeedsAttention('Jenkins 参数填写结果验证失败')
        # Keep BUILD_TYPE / SIGNING_REPO_BRANCH / other parameters unchanged.
        print(f'[Jenkins/gmx] ENTITIES={country} SYNC_UPLOAD=true CUSTOM_FOLDER={target}', flush=True)

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
                self.fill_gmx_parameters(country, project)
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

    def wait_result(self, job_url, number, minutes=None, *, watch_callback=False):
        deadline = time.monotonic() + 60 * float(minutes or self.conf.get('build_timeout_minutes', 180))
        start = time.monotonic()
        action('JENKINS', f'{self.site} 正在监控 Build #{number}: {job_url}{number}/')
        build_url = job_url + str(number) + '/'
        while time.monotonic() < deadline:
            # Browser reads Jenkins' console page, never calls Jenkins REST APIs.
            code = self.visit(build_url + 'consoleText', timeout=120000)
            if code == 200:
                operation('PW', 'page.locator(body).evaluate(innerText 后30000字符) [正文不会直接打印]')
                end = self.page.locator('body').evaluate('(e) => e.innerText.slice(-30000)')
                result = final_result_from_console(end)
                if result:
                    print(f'[Jenkins/{self.site}] Build #{number} => {result}', flush=True)
                    return result, end
            # If Jenkins consoleText is blocked, use DOM status indicator on numbered build.
            code = self.visit(build_url)
            if code not in (200,):
                raise NeedsAttention(f'Build #{number} 页面 HTTP {code}')
            # Jenkins 2.x modern UI uses SVG icons, while older instances use
            # #build-status-icon / .build-status-icon. Only inspect this build's
            # HEADER status controls; never interpret other jobs' green icons.
            selector = ("#build-status-icon, .build-status-icon, "
                        ".jenkins-build-status, .page-headline svg.svg-icon, "
                        "#main-panel h1 svg.svg-icon, h1 svg.icon-success, "
                        "h1 svg.icon-failure, h1 svg.icon-aborted, h1 svg.icon-unstable")
            operation('PW', f'page.locator({selector!r}) 检查当前 Build 的新旧版状态图标')
            icons = self.page.locator(selector)
            for index in range(min(icons.count(), 6)):
                info = icons.nth(index).evaluate(
                    '(el) => [el.className?.baseVal || el.className, '
                    'el.getAttribute("title"), el.getAttribute("aria-label"), '
                    'el.getAttribute("alt"), el.outerHTML.slice(0,700)].join(" ")')
                status = result_from_status_markup(info)
                if status:
                    action('JENKINS', f'{self.site} Build #{number} 页面图标确认终态: {status}')
                    return status, ''
            if watch_callback:
                # Plugin release-build is a two-part Jenkins pipeline. The
                # lightweight Manual Trigger (#1) launches a linked Manual
                # Callback (#2). Some corporate console pages omit the final
                # "Finished:" line, so don't keep polling #1 after the
                # explicitly related callback is already visible.
                child = self.find_callback(job_url, number)
                if child is not None:
                    action('CALLBACK', f'{self.site} Build #{number} 已创建关联 Callback #{child}；切换监控 #{child}')
                    return 'DELEGATED', str(child)
            action('WAIT', f'{self.site} Build #{number} 状态尚未确认，已等待 {int(time.monotonic()-start)}s'
                           + ('（同时检查关联 Callback）' if watch_callback else ''))
            time.sleep(self.poll)
        raise TimeoutError(f'Jenkins Build #{number} 在 {minutes or self.conf.get("build_timeout_minutes")} 分钟内未结束: {build_url}')

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
