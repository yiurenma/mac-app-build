"""Opt-in GMX Jenkins parameterized build checks."""
from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit
import re

from .config import DATA, load_config
from .jenkins import JenkinsBrowser, NeedsAttention
from .telemetry import safe_text
from .diagnose import parse_target

BRANCH = re.compile(r'^upload/[A-Za-z0-9][A-Za-z0-9._/-]{0,125}$')
RUN_RESULTS = {'SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'}
def validate_branch(raw):
    if not isinstance(raw, str) or not BRANCH.fullmatch(raw) or '..' in raw or '//' in raw or raw.endswith('/'):
        raise ValueError('请输入合法 GMX 分支，例如 upload/UKPBB-203745-V8')
    if any(p.startswith('-') for p in raw.split('/')):
        raise ValueError('分支路径段不能以 - 开头')
    return raw


def validate_options(body):
    branch = validate_branch(body.get('branch'))
    country = body.get('country', 'UK')
    project = body.get('project', 'PayByBank')
    if not isinstance(country, str) or not re.fullmatch(r'(?:[A-Z]{2,4}|NOT_SET)', country):
        raise ValueError('Country 格式不合法，例如 UK')
    if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}', project):
        raise ValueError('Project 格式不合法')
    settings = load_config()['jenkins'].get('gmx_defaults', {}).copy()
    for name in ('SYNC_UPLOAD','SKIP_TEST','FORCE','KEEP_ALIVE'):
        key = name.lower()
        value = body.get(key, settings.get(name, False))
        if type(value) is not bool:
            raise ValueError(f'{name} 必须是布尔值')
        settings[name] = value
    for name, key in [('BUILD_TYPE','build_type'),('SIGNING_REPO_BRANCH','signing_repo_branch'),('CUSTOM_FOLDER','custom_folder')]:
        value = body.get(key, settings.get(name, ''))
        if not isinstance(value, str) or len(value) > 100 or (value and (value.startswith('-') or '..' in value)):
            raise ValueError(f'{name} 格式不合法')
        if name != 'CUSTOM_FOLDER' and not value:
            raise ValueError(f'{name} 不能为空')
        settings[name] = value
    return branch,country,project,settings


def job_candidates(branch, config):
    """Both %2F and %252F are legitimate Jenkins routing presentations."""
    base = config['jenkins']['gmx_parent'].rstrip('/') + '/'
    encoded = quote(branch, safe='')
    return [base + encoded + '/', base + encoded.replace('%2F', '%252F') + '/']


def validate_job_url(raw, branch, config):
    """An optional browser-copied URL, restricted to this exact GMX branch."""
    target = parse_target(raw, config)
    if target.site != 'gmx' or target.number is not None:
        raise ValueError('请提供 GMX 分支 Job URL，不是某次 Build URL')
    suffix = target.job_url[len(config['jenkins']['gmx_parent'].rstrip('/') + '/'):].strip('/')
    if unquote(unquote(suffix)) != branch:
        raise ValueError('GMX Job URL 与输入的分支名不一致')
    return target.job_url


def gmx_build_test(branch,country,project,settings,emit,*,config=None,job_url='',factory=JenkinsBrowser):
    branch=validate_branch(branch)
    config=config or load_config()
    if job_url:
        job_url=validate_job_url(job_url,branch,config)
    with factory(config,DATA,DATA/'live-tests'/'jenkins', 'gmx',visible=True,manual_login=True) as browser:
        browser.gmx_settings=settings
        emit(f'[START] GMX Jenkins 真实构建测试；branch={branch}')
        emit('[WARNING] 此测试将触发 Jenkins Build，可能消耗 CI 资源，不是只读诊断')
        if job_url:
            candidates=[job_url]
        else:
            candidates=job_candidates(branch,config)
        chosen=None
        for candidate in candidates:
            status=browser.visit(candidate,timeout=30000)
            emit(f'[JOB] {candidate} HTTP={status}')
            if status==200 and '404' not in browser.page.title():
                chosen=candidate;break
            if status in (401,403):
                raise NeedsAttention(f'GMX Job HTTP {status}: 检查权限')
        if not chosen:
            raise NeedsAttention('GMX 分支 Job 不存在，检查 GitHub 分支是否已被 Jenkins 扫描；未触发任何 Build')
        mode=browser.mode(chosen)
        emit(f'[MODE] Jenkins 当前入口={mode}')
        init_number=None
        if mode=='build':
            # A repeated test must never initiate another bootstrap build when a
            # prior attempt already exists but parameters have not appeared.
            browser.visit(chosen)
            existing = sorted(browser.build_numbers(chosen))
            if existing:
                raise NeedsAttention(
                    f'GMX Job 已有 Build {existing[-10:]}，但参数化入口尚未出现；'
                    '请先检查已有初始化 Build 或等待 Job 配置更新。为避免重复触发，本次未点击 Build')
            emit('[STEP 1] 第一次普通 Build：初始化参数化 Job（可能被 Disabled）')
            before=browser.request_build(chosen,with_parameters=False)
            init_number=browser.detect_new_build(chosen,before)
            emit(f'[INIT] Build #{init_number} 已触发，等待 Build with Parameters 出现（不要求初始化 SUCCESS）')
            browser.wait_parameter_mode(chosen)
            emit('[INIT] Build with Parameters 已出现；首次 Build 初始化结束')
        elif mode=='parameters':
            emit('[INIT] 已有 Build with Parameters，跳过首次 Build，避免重复初始化')
        else:
            raise NeedsAttention('页面没有唯一可识别的 Build 或 Build with Parameters；不执行构建')
        emit('[STEP 2] 填写并验证 ENTITIES / SYNC_UPLOAD / CUSTOM_FOLDER / BUILD_TYPE 等参数')
        emit(f'[PARAMETERS] country={country} project={project} settings={settings}')
        before=browser.request_build(chosen,with_parameters=True,country=country,project=project)
        formal=browser.detect_new_build(chosen,before)
        emit(f'[BUILD] 正式 Build #{formal} 已创建：{chosen}{formal}/')
        emit('[STOP] GMX 正式 Build 已触发并出现 Build Number；按测试范围立即停止，不等待结果')
        emit(f'[RESULT] TRIGGERED · GMX Build #{formal}: {chosen}{formal}/')
        emit('[NOTE] TRIGGERED 不代表 SUCCESS；本测试不监控构建结束、不校验上传')
        return {'verdict':'PASS','kind':'gmx-build','branch':branch,
                'job_url':chosen,'init_build_number':init_number,'build_number':formal,
                'actual':'TRIGGERED', 'build_url':chosen+str(formal)+'/',
                'result_verified':False,'upload_verified':False}

