"""Read only target tarball checksums from the current build's Nexus JSON output."""
import json
import re
from urllib.parse import unquote, urlsplit
from .versions import ARTIFACTS


def upload_rows(console):
    # Jenkins timestamper prefixes are outside the JSON, not part of its values.
    clean = re.sub(r'\x1b\[[0-9;]*m', '', console)
    clean = re.sub(r'(?m)^[ \t]*(?:\[\d{2}:\d{2}:\d{2}(?:\.\d+)?\]|\d{2}:\d{2}:\d{2}(?:\.\d+)?)[ \t]*', '', clean)
    blocks = list(re.finditer(r'(?m)^[ \t]*::group::Nexus upload output[ \t]*$', clean))
    if not blocks:
        raise ValueError('当前 Plugin Build 缺少 Nexus upload output JSON')
    result = []
    decoder = json.JSONDecoder()
    for block in blocks:
        payload = clean[block.end():].lstrip()
        try:
            data, _ = decoder.raw_decode(payload)
        except json.JSONDecodeError as exc:
            raise ValueError('Nexus upload output JSON 不完整或无效') from exc
        rows = data.get('upload_results') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError('Nexus upload output 缺少 upload_results 数组')
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('upload_results 条目不是 JSON 对象')
        result.extend(rows)
    return result


def parse_upload_metadata(console):
    versions = set()
    for row in upload_rows(console):
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
    return {'version': version, 'hashes': parse_upload_hashes(console, version)}


def parse_upload_hashes(console, version):
    found = {name: set() for name in ARTIFACTS}
    for row in upload_rows(console):
        url = row.get('artifactUrl', '')
        if not isinstance(url, str):
            continue
        basename = unquote(urlsplit(url).path).rsplit('/', 1)[-1]
        for name in ARTIFACTS:
            if basename != f'{name}-{version}.tar.gz':
                continue
            if row.get('version') != version:
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
