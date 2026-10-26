"""Read only target tarball checksums from the current build's Nexus JSON output."""
import json
import re
from urllib.parse import unquote, urlsplit
from .versions import ARTIFACTS

_CLOCK = r'\d{2}:\d{2}:\d{2}(?:\.\d+)?'
_STAMP = rf'(?:\d{{4}}-\d{{2}}-\d{{2}}[T ]{_CLOCK}(?:Z|[+-]\d{{2}}:?\d{{2}})?|{_CLOCK})'
_PREFIX = re.compile(rf'(?m)^[ \t]*(?:\[{_STAMP}\]|{_STAMP})[ \t]*')


def normalize_console(console):
    # Jenkins timestamper prefixes are outside the JSON, not part of its values.
    clean = re.sub(r'\x1b\[[0-9;]*m', '', console)
    clean = _PREFIX.sub('', clean)
    return clean.replace('\r\n', '\n')


def console_lines(console):
    """Iterate without allocating a list/copy of every line in a large log."""
    start = 0
    while start < len(console):
        end = console.find('\n', start)
        if end < 0:
            yield console[start:]
            return
        yield console[start:end]
        start = end + 1


def upload_rows(console, emit=None):
    result, groups, rejected_markers = [], 0, 0
    buffer = None
    depth, in_string, escaped, started = 0, False, False, False
    for raw in console_lines(console):
        if buffer is None:
            # Most lines are unrelated: avoid timestamp/ANSI regex work on them.
            if 'Nexus upload output' not in raw:
                continue
            if normalize_console(raw).strip() != '::group::Nexus upload output':
                rejected_markers += 1
                continue
            groups += 1
            if emit:
                emit(f'[SEARCH] 找到 Nexus upload output 分组 #{groups}，正在读取 JSON')
            buffer = []
            depth, in_string, escaped, started = 0, False, False, False
            continue
        line = normalize_console(raw).strip()
        if not started and not line:
            continue
        if not started and not line.startswith('{'):
            raise ValueError('Nexus upload output 后不是 JSON 对象')
        buffer.append(line)
        for ch in line:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == '\\':
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch in '{[':
                depth += 1
                started = True
            elif ch in '}]':
                depth -= 1
        if started and depth <= 0 and not in_string:
            try:
                data = json.loads('\n'.join(buffer))
            except json.JSONDecodeError as exc:
                raise ValueError('Nexus upload output JSON 无效') from exc
            rows = data.get('upload_results') if isinstance(data, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('Nexus upload output 缺少有效 upload_results 数组')
            result.extend(rows)
            buffer = None
    if not groups:
        if emit:
            emit(f'[SEARCH] 未识别 Nexus 分组；含标题但格式不匹配的行={rejected_markers}')
        raise ValueError('当前 Plugin Build 缺少 Nexus upload output JSON')
    if buffer is not None:
        raise ValueError('Nexus upload output JSON 不完整')
    if emit:
        emit(f'[SEARCH] 已搜索完整日志；Nexus upload output 分组={groups}')
        emit(f'[JSON] upload_results 条目={len(result)}；按插件名筛选 .tar.gz，排除附属文件')
    return result


def parse_upload_metadata(console, emit=None):
    versions = set()
    rows = upload_rows(console, emit=emit)
    for row in rows:
        url = row.get('artifactUrl', '')
        if not isinstance(url, str):
            continue
        basename = unquote(urlsplit(url).path).rsplit('/', 1)[-1]
        for name in ARTIFACTS:
            match = re.fullmatch(re.escape(name) + r'-(.+)\.tar\.gz', basename)
            if match:
                versions.add(match[1])
    if len(versions) != 1:
        raise ValueError('Nexus 输出的两个目标 tar.gz 必须有唯一一致版本')
    version = next(iter(versions))
    return {'version': version, 'hashes': hashes_from_rows(rows, version)}


def parse_upload_hashes(console, version):
    return hashes_from_rows(upload_rows(console), version)


def parse_upload_downloads(console):
    """Preserve exact successful-upload URLs for independent byte verification.

    Parsing a URL does not establish trusted origin, availability or ownership.
    The caller must first attribute this console to the exact successful Build.
    """
    metadata = parse_upload_metadata(console)
    version = metadata['version']
    rows = upload_rows(console)
    result = {}
    for name in ARTIFACTS:
        urls = {row['artifactUrl'] for row in rows
                if isinstance(row.get('artifactUrl'), str)
                and unquote(urlsplit(row['artifactUrl']).path).rsplit('/', 1)[-1]
                == f'{name}-{version}.tar.gz'}
        if len(urls) != 1:
            raise ValueError('每个目标产物必须有唯一下载地址，不能猜测镜像或最新版链接')
        result[name] = {'url': next(iter(urls)), 'version': version,
                        'sha256': metadata['hashes'][name]}
    return result


def hashes_from_rows(rows, version):
    found = {name: set() for name in ARTIFACTS}
    for row in rows:
        url = row.get('artifactUrl', '')
        if not isinstance(url, str):
            continue
        basename = unquote(urlsplit(url).path).rsplit('/', 1)[-1]
        for name in ARTIFACTS:
            if basename != f'{name}-{version}.tar.gz':
                continue
            # Nexus reports the release/base version while the tarball filename
            # includes the Jenkins snapshot build suffix. Keep the exact full
            # filename match above; only accept this known metadata convention.
            base_version = re.sub(r'-SNAPSHOT\.[0-9]+$', '', version)
            if row.get('version') not in (version, base_version):
                raise ValueError(f'{name} Nexus 版本与当前 Artifact Version 不一致')
            digest = row.get('artifactSHA256')
            if not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
                raise ValueError(f'{name} artifactSHA256 必须为 64 位十六进制')
            alias = row.get('artifactSHA')
            if alias is not None and (not isinstance(alias, str) or alias.lower() != digest.lower()):
                raise ValueError(f'{name} artifactSHA 与 artifactSHA256 冲突')
            found[name].add(digest.lower())
    if any(len(values) != 1 for values in found.values()):
        raise ValueError('两个目标 tar.gz 必须各有唯一 SHA256（当前日志缺失或存在冲突）')
    return {name: next(iter(found[name])) for name in ARTIFACTS}
