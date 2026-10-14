"""APP link lookup over Jenkins' own HTTP endpoints with a user API token.

Same evidence chain and parsers as the browser path (jenkins_links): the fixed GMX
build's full HTML console -> the first successful upload-to-testfairy/master build ->
its full HTML console -> the single testfairy.com/join link. Only the transport differs:
no browser, no SSO session. Any failure raises NeedsAttention so the caller can fall
back to the browser.
"""
import base64
import ssl
import urllib.error
import urllib.request

from .jenkins import NeedsAttention
from .jenkins_links import _build, _exact_target, join_url_from_html, upload_build_from_html

MAX_BYTES = 64 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # a redirect usually means an SSO login page; never follow it


def http_fetch(user, token, *, timeout=30, ca_bundle=None):
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
    auth = 'Basic ' + base64.b64encode(f'{user}:{token}'.encode()).decode()

    def fetch(url):
        request = urllib.request.Request(url, headers={'Authorization': auth, 'Accept': 'text/html'})
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise NeedsAttention('Jenkins 日志过大，Token 方式不读取')
                charset = response.headers.get_content_charset() or 'utf-8'
                return response.status, response.geturl(), body.decode(charset, 'replace')
        except urllib.error.HTTPError as exc:
            return exc.code, url, ''
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise NeedsAttention(f'无法连接 Jenkins（{type(exc).__name__}）') from None
    return fetch


def api_lookup(source_job_url, source_number, fetch, emit=lambda message: None):
    source = _build(source_job_url, source_number)

    def read(url, name):
        status, final, html = fetch(url)
        if status in (401, 403):
            raise NeedsAttention(f'Jenkins 拒绝了 Token（HTTP {status}），可能 Token 无效、无权限或公司禁用了 Token 访问')
        if status in (301, 302, 303, 307, 308):
            raise NeedsAttention('Jenkins 把 Token 请求重定向到登录页，公司可能只允许浏览器 SSO')
        if status != 200 or not _exact_target(final, url):
            raise NeedsAttention(f'{name} 读取失败（HTTP {status}）')
        emit(f'[APP-LINK] {name}: Token 读取 HTTP=200，HTML字符数={len(html)}')
        return html

    full = source['build_url'] + 'consoleFull'
    downstream = upload_build_from_html(read(full, 'source_full'), full)
    emit(f'[APP-LINK] first_upload_segment: 上传构建 #{downstream["number"]}')
    full2 = downstream['build_url'] + 'consoleFull'
    share = join_url_from_html(read(full2, 'downstream_full'), full2)
    return {'share_url': share, 'source_job_url': source['job_url'],
            'source_build_number': source['number'], 'source_build_url': source['build_url'],
            'downstream_job_url': downstream['job_url'], 'downstream_build_number': downstream['number'],
            'downstream_build_url': downstream['build_url']}
