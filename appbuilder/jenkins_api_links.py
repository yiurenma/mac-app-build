"""APP link lookup with the Jenkins API token: plain-text logs plus Jenkins' own records.

The browser path (jenkins_links) parses the rendered console HTML. The HTML Jenkins
serves to a non-browser client is not always the same, so the token path does not parse
HTML at all:

1. the fixed GMX build's consoleText -> the FIRST "[Pipeline] build (... upload-to-
   testfairy » master)" segment -> "Starting building: <names> #N" and its own
   "Build <names> #N completed: SUCCESS" line;
2. Jenkins' record of that upload build (api/json) must name exactly this GMX build as its
   upstream cause and be finished with SUCCESS;
3. the upload build's console page (HTML): the join link is only a hyperlink there, its
   plain text shows just the label. The same parser and rules as the browser path apply.

Anything missing or ambiguous raises NeedsAttention; the caller then falls back to the browser.
"""
import re
from urllib.parse import quote

from .jenkins import NeedsAttention
from .jenkins_links import _BUILD_START, _COMPLETED, _TARGET, _build, join_url_from_html

_STARTING = re.compile(r'Starting building:\s*(?P<name>[^\n#]+?)\s*#(?P<number>[1-9][0-9]*)\b')
_FINISHED = re.compile(r'\bBuild\s+(?P<name>[^\n#]+?)\s*#(?P<number>[1-9][0-9]*)\s+completed:\s*(?P<result>[A-Z_]+)', re.I)


def _names(display):
    parts = [p.strip() for p in display.split('»')]
    if len(parts) < 2 or parts[-2:] != ['upload-to-testfairy', 'master'] or not all(parts):
        raise NeedsAttention('上传构建名称不是 … » upload-to-testfairy » master')
    return parts


def upload_build_from_text(text, root):
    """(job_url, number) of the first upload-to-testfairy/master segment that succeeded."""
    first = _TARGET.search(text)
    if not first:
        raise NeedsAttention('纯文本日志未找到首个 » upload-to-testfairy » master) 构建段')
    following = _BUILD_START.search(text, first.end())
    segment = text[first.end():following.start() if following else len(text)]
    start = _STARTING.search(segment)
    done = _FINISHED.search(segment)
    if not start or not done:
        raise NeedsAttention('首个上传段缺少 Starting building 或完成行')
    if done['number'] != start['number'] or _names(done['name']) != _names(start['name']):
        raise NeedsAttention('首个上传段开始行与完成行的构建不一致')
    if done['result'].upper() != 'SUCCESS' or not _COMPLETED.search(segment):
        raise NeedsAttention('首个上传段未 completed: SUCCESS')
    job = root + ''.join('job/' + quote(name, safe='') + '/' for name in _names(start['name']))
    return job, int(start['number'])


def api_lookup(source_job_url, source_number, http, emit=lambda message: None):
    from .jenkins_api import _auth_check, jenkins_root

    source = _build(source_job_url, source_number)

    def text(url, name):
        status, _, _, raw = http.request('GET', url, accept='text/plain')
        _auth_check(status)
        if status != 200:
            raise NeedsAttention(f'{name} 读取失败（HTTP {status}）')
        body = raw.decode('utf-8', 'replace')
        emit(f'[APP-LINK] {name}: Token 读取纯文本日志 HTTP=200，字符数={len(body)}')
        return body

    job_url, number = upload_build_from_text(text(source['build_url'] + 'consoleText', 'source_log'),
                                             jenkins_root(source['job_url']))
    build_url = job_url + f'{number}/'
    emit(f'[APP-LINK] first_upload_segment: 上传构建 #{number}')

    status, info = http.get_json(source['job_url'], tree='fullName')
    full_name = (info or {}).get('fullName') if status == 200 else None
    status, upload = http.get_json(build_url, tree='building,result,actions[causes[upstreamProject,upstreamBuild]]')
    if status != 200 or not isinstance(upload, dict):
        raise NeedsAttention(f'上传构建记录读取失败（HTTP {status}）')
    if upload.get('building') is not False or upload.get('result') != 'SUCCESS':
        raise NeedsAttention('上传构建未以 SUCCESS 结束')
    causes = [c for holder in upload.get('actions') or [] for c in (holder or {}).get('causes') or []
              if isinstance(c, dict)]
    if not any(c.get('upstreamBuild') == source['number'] and full_name and c.get('upstreamProject') == full_name
               for c in causes):
        raise NeedsAttention('上传构建的上游记录不是本次 GMX 构建，拒绝采用')
    emit('[APP-LINK] Jenkins 记录确认：该上传构建由本次 GMX 构建触发并成功')

    page = build_url + 'consoleFull'
    status, final, _, raw = http.request('GET', page, accept='text/html')
    _auth_check(status)
    if status != 200:
        raise NeedsAttention(f'上传构建日志页读取失败（HTTP {status}）')
    html = raw.decode('utf-8', 'replace')
    emit(f'[APP-LINK] upload_page: Token 读取上传构建日志页 HTTP=200，HTML字符数={len(html)}')
    share = join_url_from_html(html, page)
    return {'share_url': share, 'source_job_url': source['job_url'],
            'source_build_number': source['number'], 'source_build_url': source['build_url'],
            'downstream_job_url': job_url, 'downstream_build_number': number,
            'downstream_build_url': build_url}
