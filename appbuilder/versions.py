from __future__ import annotations
from datetime import datetime
import re
from urllib.parse import quote

MAJOR_RE = re.compile(r"^[0-9]+\.[0-9]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
ARTIFACTS = ("PayByBankDomainModelPluginKit", "PayByBankJourneyPluginKit")


def validate_args(major, plugin_source, gmx_source, country, project):
    if not MAJOR_RE.fullmatch(major):
        raise ValueError("Plugin Version 必须为 1.3 这样的主次版本号")
    for b in [plugin_source, gmx_source]:
        if not b or not BRANCH_RE.fullmatch(b) or b.startswith(('-', '.')) or '..' in b or '//' in b or b.endswith(('/', '.')):
            raise ValueError(f"无效源分支: {b!r}")
    for label, v in [('country', country), ('project', project)]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", v):
            raise ValueError(f"{label} 只能包含字母、数字、下划线、连字符且不可为空")


def timestamp_now():
    return datetime.now().strftime("%Y%m%d%H%M")


def branch_names(major, stamp):
    if not MAJOR_RE.fullmatch(major) or not re.fullmatch(r"\d{12}", stamp):
        raise ValueError("无效大版本或时间戳")
    v = f"{major}.{stamp}"
    return {"plugin_version": v, "plugin_branch": f"release/{v}",
            "gmx_branch": f"upload/auto{stamp}"}


def job_urls(config, plugin_branch, gmx_branch):
    parent1 = config['jenkins']['plugin_parent'].rstrip('/') + '/'
    parent2 = config['jenkins']['gmx_parent'].rstrip('/') + '/'
    plugin_job = plugin_branch.replace('/', '_').replace('.', '_')
    gmx_job = quote(gmx_branch, safe='')
    return {'plugin_job': parent1 + plugin_job + '/', 'gmx_job': parent2 + gmx_job + '/'}


def folder(country, project):
    return f"0_A_{country}_{project}"


def parse_artifacts(paths):
    """Only exact build-universal tarballs; never prefer Last Successful Artifacts."""
    hits = {}
    for p in paths:
        p = p.split('?', 1)[0]
        if '/build-universal/' not in ('/' + p.lstrip('/')):
            continue
        basename = p.rstrip('/').rsplit('/', 1)[-1]
        for name in ARTIFACTS:
            m = re.fullmatch(re.escape(name) + r'-(.+)\.tar\.gz', basename)
            if m:
                hits.setdefault(name, set()).add(m.group(1))
    if set(hits) != set(ARTIFACTS):
        raise ValueError(f"没有找到两个 Plugin 的 tar.gz Artifacts: {hits}")
    if any(len(v) != 1 for v in hits.values()):
        raise ValueError(f"Artifact 出现多个版本: {hits}")
    one, two = [next(iter(hits[k])) for k in ARTIFACTS]
    if one != two:
        raise ValueError(f"两个 Plugin Artifact 版本不一致: {one} / {two}")
    return one
