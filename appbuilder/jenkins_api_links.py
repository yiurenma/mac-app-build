"""APP link lookup over Jenkins' own HTTP endpoints with a user API token.

Same evidence chain and parsers as the browser path (jenkins_links): the fixed GMX
build's full HTML console -> the first successful upload-to-testfairy/master build ->
its full HTML console -> the single testfairy.com/join link. Only the transport differs:
no browser, no SSO session. Any failure raises NeedsAttention so the caller can fall
back to the browser.
"""
from .jenkins import NeedsAttention
from .jenkins_links import _build, _exact_target, join_url_from_html, upload_build_from_html

MAX_BYTES = 64 * 1024 * 1024


def http_fetch(user, token, *, timeout=30, ca_bundle=None):
    from .jenkins_api import JenkinsHttp

    http = JenkinsHttp(user, token, timeout=timeout, ca_bundle=ca_bundle)

    def fetch(url):
        try:
            status, final, headers, raw = http.request('GET', url, accept='text/html')
        except NeedsAttention as exc:
            raise NeedsAttention(str(exc)) from None
        charset = 'utf-8'
        for key, value in headers.items():
            if key.lower() == 'content-type' and 'charset=' in value:
                charset = value.split('charset=', 1)[1].split(';')[0].strip() or 'utf-8'
        return status, final, raw.decode(charset, 'replace')
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
