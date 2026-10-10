"""Read-only, one-shot Jenkins URL diagnostics; never click any Build control."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, unquote
import json
import re
import time

from .jenkins import JenkinsBrowser, final_result_from_console
from .config import DATA, load_config
from .telemetry import safe_text


@dataclass(frozen=True)
class Target:
    site: str
    job_url: str
    number: int | None


def parse_target(url: str, config: dict, *, number: int | None = None) -> Target:
    """Strict allow-list: only direct descendants of configured Jenkins job parents.

    A user URL must identify a branch job or a numbered build. No arbitrary
    destinations, URL credentials, requests with parameters or non-HTTPS URLs.
    """
    if not isinstance(url, str) or len(url) > 2048 or not url.strip() or any(c.isspace() for c in url):
        raise ValueError('请粘贴有效 Jenkins HTTPS Job/Build URL（不含空格）')
    s = urlsplit(url.strip())
    if s.scheme != 'https' or not s.hostname or s.username or s.password or s.query or s.fragment or s.port not in (None, 443):
        raise ValueError('只支持不含凭据、查询参数和锚点的 HTTPS Jenkins URL')
    if number is not None and (type(number) is not int or number <= 0 or number > 2_000_000_000):
        raise ValueError('Build Number 必须是正整数')
    if any(p in ('.', '..') for p in s.path.split('/')) or '\\' in s.path:
        raise ValueError('URL 路径不能包含相对路径')
    for site in ('plugin', 'gmx'):
        base = urlsplit(config['jenkins'][site + '_parent'])
        prefix = base.path.rstrip('/') + '/'
        if s.scheme != base.scheme or s.netloc.lower() != base.netloc.lower() or not s.path.startswith(prefix):
            continue
        suffix = s.path[len(prefix):].strip('/')
        parts = suffix.split('/')
        # E.g. /job/release_1_3_.../2/ or /job/upload%2Fauto.../2/
        if not suffix or len(parts) not in (1, 2) or not re.fullmatch(r'[A-Za-z0-9%_.-]{1,180}', parts[0]):
            raise ValueError('请输入具体分支 Job URL 或它的 /数字/ Build URL')
        # Jenkins/Chrome may expose a slash in a multibranch job name as
        # %2F (encoded slash) or %252F (encoded literal "%2F"). Accept both
        # EXACTLY, preserving the copied URL for navigation; reject arbitrary
        # percent escapes, traversal and triple-encoding.  Do not unquote() the
        # name: changing %252F to %2F can identify another Jenkins resource.
        if not re.fullmatch(r'(?:[A-Za-z0-9_.-]|%2[fF]|%252[fF])+', parts[0]):
            raise ValueError('Job 名称仅支持普通字符及 Jenkins 分支编码 %2F / %252F；请复制分支 Job 地址')
        unpacked = unquote(unquote(parts[0]))
        if any(segment in ('.', '..') for segment in unpacked.split('/')):
            raise ValueError('Job URL 包含不允许的路径段')
        if len(parts) == 2:
            if not re.fullmatch(r'[1-9][0-9]{0,9}', parts[1]):
                raise ValueError('Build URL 结尾应为 /数字/；请检查 URL')
            parsed = int(parts[1])
            if number is not None and parsed != number:
                raise ValueError('URL 中的 Build Number 和输入编号不一致')
            number = parsed
        job = f'{s.scheme}://{s.netloc}{prefix}{parts[0]}/'
        return Target(site, job, number)
    raise ValueError('URL 不属于 config.yaml 配置的 Plugin/GMX Jenkins Job，请检查地址')


def expectation(actual: str, expected: str, site: str) -> tuple[str, str]:
    if actual == 'UNKNOWN':
        return 'UNKNOWN', '目前证据不足；不启动构建。检查 URL、SSO 登录状态或页面状态元素。'
    if actual == expected:
        nxt = ('Plugin Callback 成功后应读取两个 Artifacts，再进入 GMX；这是状态诊断，不代表产物已校验。'
               if site == 'plugin' else 'GMX Build 已成功；上传是否完成还须单独校验。')
        return 'PASS', nxt
    return 'FAIL', f'实际结果 {actual} 不符合预期 {expected}；不执行任何自动构建动作。'


def run_diagnosis(url: str, *, number: int | None = None, expected='SUCCESS', visible=True,
                  config: dict | None = None, factory=JenkinsBrowser, emit=None) -> dict:
    """Read-only: inspect job history, numbered build heading, optional console.

    In all cases no checkout, Git push, Jenkins click or build trigger happens.
    Keep a small, sanitized report on disk, independent of build task DB.
    """
    config = config or load_config()
    target = parse_target(url, config, number=number)
    if expected not in ('SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'):
        raise ValueError('预期结果必须为 Jenkins 标准终态')
    began = time.monotonic()
    lines: list[str] = []

    def log(message):
        clean = safe_text(message)
        lines.append(clean)
        if emit:
            emit(clean)
        else:
            print(clean, flush=True)

    log(f'[DIAG] READ ONLY · {target.site.upper()} · 不触发 Jenkins 构建，不操作 Git')
    log(f'[TARGET] Job URL: {target.job_url}')
    if '%252f' in target.job_url.lower():
        log('[URL] Jenkins 采用双重编码 %252F：保留浏览器原始地址，不改写为 %2F')
    elif '%2f' in target.job_url.lower():
        log('[URL] Jenkins 采用分支编码 %2F：保留浏览器原始地址')
    log(f'[TARGET] Build Number: {target.number if target.number else "自动发现最新编号"}; expected={expected}')
    log('[BROWSER] 正在启动可见 Chrome；如需 SSO，请直接在该窗口人工登录，最多等待 180 秒')
    folder = DATA / 'diagnostics' / target.site
    # Site-specific screenshots / reports also support simultaneous Plugin + GMX tests.
    try:
        with factory(config, DATA, folder, target.site, visible=True, manual_login=True) as browser:
            log('[STEP 1/4] 加载指定 Jenkins Job 页面（只读）')
            code = browser.visit(target.job_url, timeout=20000)
            log(f'[HTTP] Job page={code}; page={browser.page.url}')
            if code != 200:
                raise RuntimeError(f'Job 页面 HTTP {code}，无法诊断')
            numbers = sorted(browser.build_numbers(target.job_url))
            log(f'[SCAN] Build History IDs={numbers[-15:]}')
            n = target.number or (max(numbers) if numbers else None)
            if not n:
                raise RuntimeError('页面没有发现 Build Number；请直接粘贴 /2/ 这样的具体 Build URL')
            log(f'[TARGET] 实际检查 Build #{n}；不使用其他编号的绿色勾')
            log('[STEP 2/4] 读取 Build History 对应编号状态图标')
            history = browser._history_result(target.job_url, n)
            log(f'[RESULT] Build History #{n}: {history or "UNKNOWN（页面无可识别专属图标）"}')
            probe = getattr(browser, 'last_history_probe', None)
            if isinstance(probe, dict):
                matches = probe.get('targetLinks', 0)
                rows = probe.get('candidates', [])
                log(f'[DOM] 精确 Build #{n} 链接={matches}; 候选记录={len(rows)}; 当前页面构建链接={probe.get("pageBuildLinks", "?")}')
                for row in rows[:3]:
                    for icon in row.get('icons', [])[:4]:
                        log(f'[ICON] {icon.get("tag", "?")} {icon.get("width", "?")}x{icon.get("height", "?")} '
                            f'visible={icon.get("visible")} colors={icon.get("colors", [])[:5]}')
                if probe.get('error'):
                    log(f'[DOM-ERROR] {probe["error"]}')
                if not rows:
                    log('[HINT] 未定位到构建编号旁的状态图标；可能 Jenkins 使用未覆盖的 DOM 布局')
            log('[STEP 3/4] 读取当前 Build 详情页标题状态（只读）')
            build_url = target.job_url + str(n) + '/'
            build_http = browser.visit(build_url, timeout=20000)
            heading = browser._heading_result(n) if build_http == 200 else None
            log(f'[HTTP] Build #{n} detail={build_http}')
            log(f'[RESULT] Build #{n} heading: {heading or "UNKNOWN"}')
            console = None
            console_code = 'SKIPPED'
            if history and heading and history != heading:
                # Disagreement must not become a false success.
                actual = 'UNKNOWN'
                log(f'[CONFLICT] History={history}, Heading={heading}；终态冲突，不能确认')
            else:
                actual = history or heading
            if not actual:
                log('[STEP 4/4] 图标未提供终态；短超时检查原始 consoleText 尾部')
                try:
                    response = browser.context.request.get(build_url + 'consoleText', timeout=5000)
                    console_code = response.status
                    if (response.status == 200 and urlsplit(response.url).hostname == urlsplit(build_url).hostname
                            and urlsplit(response.url).path == urlsplit(build_url).path + 'consoleText'):
                        body = response.text()[-4096:]
                        console = final_result_from_console(body)
                        content_type = getattr(response, 'headers', {}).get('content-type', 'unknown')
                        log(f'[HTTP] consoleText={console_code}; type={content_type}; tail bytes checked={len(body.encode("utf-8"))}')
                        if 'html' in content_type.lower():
                            log('[HINT] consoleText 返回 HTML 而不是普通构建日志；HTTP 200 不代表已获取完整 Console')
                        log(f'[RESULT] Console Finished marker: {console or "NOT FOUND"}')
                    else:
                        log(f'[HTTP] consoleText={console_code}; redirect={urlsplit(response.url).hostname != urlsplit(build_url).hostname}')
                except Exception as exc:
                    log(f'[HTTP] consoleText ERROR: {type(exc).__name__}; 无需等待长日志')
                actual = console or 'UNKNOWN'
            else:
                log('[STEP 4/4] 已通过 #'+str(n)+' 的状态图标识别终态，跳过长 Console 日志下载')
            if actual == 'UNKNOWN':
                try:
                    screenshot = folder / f'{target.site}-build-{n}-unknown.png'
                    browser.page.screenshot(path=str(screenshot), full_page=False)
                    log(f'[SNAPSHOT] 已保存现场页面截图: {screenshot}')
                except Exception:
                    pass
            verdict, next_action = expectation(actual, expected, target.site)
            log(f'[VERDICT] {verdict} · expected={expected} · actual={actual}')
            log(f'[NEXT] {next_action}')
            output = {'verdict': verdict, 'actual': actual, 'expected': expected,
                      'site': target.site, 'build_number': n, 'job_url': target.job_url,
                      'build_url': build_url, 'history': history, 'heading': heading,
                      'console': console, 'console_http': console_code, 'next': next_action}
    except Exception as exc:
        log(f'[DIAG-ERROR] {type(exc).__name__}: {str(exc)}')
        verdict, next_action = expectation('UNKNOWN', expected, target.site)
        log('[VERDICT] UNKNOWN：请核对内网连接、页面权限与登录状态，未触发 Build')
        output = {'verdict': verdict, 'actual': 'UNKNOWN', 'expected': expected,
                  'site': target.site, 'job_url': target.job_url, 'build_number': target.number,
                  'error': type(exc).__name__ + ': ' + safe_text(str(exc)), 'next': next_action}
    log(f'[TIME] elapsed={time.monotonic()-began:.1f}s')
    output['logs'] = lines
    output['at'] = datetime.now().isoformat(timespec='seconds')
    # No tasks/state mutation. Local diagnostics are safe to delete.
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(output, ensure_ascii=False, indent=2)
    (folder / 'last-result.json').write_text(payload, encoding='utf-8')
    # Retain the legacy path for existing CLI users; site-specific is canonical.
    (DATA / 'diagnostics' / 'last-result.json').write_text(payload, encoding='utf-8')
    return output
