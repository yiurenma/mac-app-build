from __future__ import annotations
from datetime import datetime
import re
from urllib.parse import quote

MAJOR_RE = re.compile(r"^[0-9]+\.[0-9]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
ARTIFACTS = ("PayByBankDomainModelPluginKit", "PayByBankJourneyPluginKit")

# Only the shipped Plugin project has a known release -> feature folder migration.
PLUGIN_RELEASE_PARENT = ('https://mobile-premier01-jenkins.build-mobile.iwpb.euw2.prod.aws.cloud.hsbc/'
                         'job/IWPB_Transaction_Banking/job/Domestic_Payment_and_Transfer_Mobile_Global_12824743/'
                         'job/Lib/job/mobile_paybybank-ios-plugin/job/release-build/job/')
PLUGIN_FEATURE_PARENT = PLUGIN_RELEASE_PARENT.replace('/release-build/', '/feature-build/')


def plugin_parent_for_branch(parent, branch):
    if branch.startswith('feature/') and parent.rstrip('/') + '/' == PLUGIN_RELEASE_PARENT:
        return PLUGIN_FEATURE_PARENT
    return parent



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


def gmx_issue_key(source, explicit=''):
    """Use an explicit issue or the single unambiguous issue in the source ref."""
    if explicit:
        if not re.fullmatch(r'[A-Z]{1,10}-[0-9]+', explicit):
            raise ValueError('GMX 工单编号必须为大写字母-数字，例如 UKPBB-203745')
        return explicit
    keys = set(re.findall(r'(?<![A-Za-z0-9])[A-Z]{1,10}-[0-9]+(?![A-Za-z0-9])', source))
    if len(keys) > 1:
        raise ValueError('GMX 源分支含多个工单编号；请填写 GMX Issue Key（或 --gmx-issue-key），使用真实工单编号')
    return keys.pop() if keys else ''


def validate_gmx_target(branch):
    if (not re.fullmatch(r'upload/[A-Z]{1,10}-[0-9]+[A-Za-z0-9_.-]*', branch)
            or '..' in branch or branch.endswith(('.', '.lock'))):
        raise ValueError('GMX 目标分支必须为 upload/工单编号-描述；旧 upload/auto 分支需先修复')
    return branch


def branch_names(major, stamp, gmx_source='', issue_key=''):
    if not MAJOR_RE.fullmatch(major) or not re.fullmatch(r"\d{12}", stamp):
        raise ValueError("无效大版本或时间戳")
    key = gmx_issue_key(gmx_source, issue_key)
    target = f"upload/{key}-auto{stamp}" if key else f"upload/AUTO-{stamp}"
    v = f"{major}.{stamp}"
    return {"plugin_version": v, "plugin_branch": f"feature/{v}",
            "gmx_branch": validate_gmx_target(target)}


def job_urls(config, plugin_branch, gmx_branch):
    parent1 = plugin_parent_for_branch(config['jenkins']['plugin_parent'], plugin_branch).rstrip('/') + '/'
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
