"""Isolated opt-in real-world checks for GMX bootstrap and Jenkins parameterized builds.

Unlike Jenkins URL Test, Jenkins build test intentionally triggers builds.  Neither
workflow updates release task state or pushes to GitHub; bootstrap clones a fresh
single-branch sandbox and only executes there.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
import os
import re
import signal
import subprocess
import threading
import time
import uuid

from .config import DATA, load_config
from .gitops import credentials
from .jenkins import JenkinsBrowser, NeedsAttention
from .telemetry import safe_text
from .diagnose import parse_target
from .edits import update_mobile_ci, validate_lock, ensure_lock_other_entries_unchanged

BRANCH = re.compile(r'^upload/[A-Za-z0-9][A-Za-z0-9._/-]{0,125}$')
RUN_RESULTS = {'SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'}
# GMX dependencies take the EXACT Plugin artifact version, not a branch's
# BUNDLE_VERSION_SHORT or an inferred 1.3.x version.
ARTIFACT_VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z]+)*$')


def validate_artifact_version(value):
    if (not isinstance(value, str) or len(value) > 100
            or not ARTIFACT_VERSION.fullmatch(value)):
        raise ValueError('请填写完整 Plugin Artifact Version，例如 1.3.1-alpha.1-SNAPSHOT.2（不能只填 1.3）')
    return value



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
    if not isinstance(country, str) or not re.fullmatch(r'[A-Z]{2,4}', country):
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


def process_with_logs(args, *, cwd, env, emit, timeout, label):
    """No shell=True, no stderr credential dump, no unbounded silent wait."""
    emit(f'[COMMAND] {label} (cwd={cwd})')
    start = time.monotonic()
    output = []
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, errors='replace', bufsize=1, start_new_session=True)
    def reader():
        try:
            for line in proc.stdout:
                clean = safe_text(line.rstrip('\r\n'), env)
                output.append(clean)
                if len(output) > 100:
                    del output[:-50]
                emit('[OUTPUT] ' + clean[:1400])
        finally:
            proc.stdout.close()
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        while proc.poll() is None:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise TimeoutError(f'{label} 超时 ({int(elapsed)}s)')
            try:
                proc.wait(timeout=min(5, max(0.1, timeout-elapsed)))
            except subprocess.TimeoutExpired:
                emit(f'[WAIT] {label} 仍在执行，已运行 {int(time.monotonic()-start)}s')
    except BaseException:
        os.killpg(proc.pid, signal.SIGTERM)
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        raise
    finally:
        t.join(timeout=10)
    emit(f'[EXIT] {label}: code={proc.returncode}, seconds={time.monotonic()-start:.1f}')
    if proc.returncode:
        raise RuntimeError(f'{label} 失败，退出码 {proc.returncode}：' + ' | '.join(output[-3:])[:300])
    return output


def bootstrap_test(branch, emit, *, artifact_version, config=None, base_dir=None, run_process=process_with_logs):
    branch = validate_branch(branch)
    artifact_version = validate_artifact_version(artifact_version)
    config = config or load_config()
    base_dir = Path(base_dir) if base_dir else DATA / 'live-tests'
    root = base_dir / ('bootstrap-' + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    repo = root / 'gmx'
    emit(f'[BOOTSTRAP] 分支：{branch}；隔离工作目录：{repo}')
    emit('[SAFETY] 测试仅克隆指定分支并运行其 bootstrap.sh；不提交、不 push、不修改正式任务')
    url = config['github']['gmx_url']
    u = urlsplit(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('GMX GitHub URL 必须是无凭据的 HTTPS 地址')
    with credentials(config['github'].get('token_env', 'GITHUB_TOKEN')) as env:
        run_process(['git','-c','credential.helper=','clone','--depth','1','--single-branch',
                     '--no-tags','--branch',branch,'--',url,str(repo)],
                    cwd=root,env=env,emit=emit,timeout=1200,label='Git clone (read-only)')
    script=repo/'bootstrap.sh'
    if not script.is_file() or not os.access(script,os.X_OK):
        raise RuntimeError('分支根目录没有可执行的 bootstrap.sh')
    sha=subprocess.run(['git','rev-parse','HEAD'],cwd=repo,text=True,capture_output=True,check=True).stdout.strip()
    emit(f'[SOURCE] GMX HEAD={sha}; branch={branch}')
    # Use the same exact dependency transformation/lock verification as the real
    # GMX release pipeline; changes stay only in this isolated test checkout.
    json_file = repo / 'mobile-ci.json'
    lock_file = repo / 'mobile-ci.lock'
    if not json_file.is_file() or not lock_file.is_file():
        raise RuntimeError('GMX 分支缺少 mobile-ci.json 或 mobile-ci.lock，不能验证 Bootstrap 结果')
    before_lock = lock_file.read_text(encoding='utf-8')
    old_versions = update_mobile_ci(json_file, artifact_version)
    emit(f'[VERSION] 完整 Plugin Artifact Version={artifact_version}; ' +
         f'两个依赖旧版本={old_versions}')
    env=os.environ.copy()
    # Script can mutate its own isolated directory; it does not run git push.
    run_process(['ci-script','upgrade'],cwd=repo,env=env,emit=emit,timeout=3600,label='ci-script upgrade')
    lines=run_process(['./bootstrap.sh'],cwd=repo,env=env,emit=emit,timeout=3600,label='./bootstrap.sh')
    checks = validate_lock(lock_file, artifact_version)
    ensure_lock_other_entries_unchanged(before_lock, lock_file.read_text(encoding='utf-8'))
    emit(f'[LOCK] 两个 Plugin 依赖版本及 SHA256 均校验通过：{list(checks)}')
    stat=subprocess.run(['git','status','--short','--untracked-files=normal'],cwd=repo,
                        text=True,capture_output=True,check=True)
    changed = stat.stdout.splitlines()
    unexpected = [line for line in changed if line[3:] not in ('mobile-ci.json', 'mobile-ci.lock')]
    if unexpected:
        raise RuntimeError(f'Bootstrap 修改了非目标文件，已停止测试：{unexpected[:8]}')
    for line in changed[:40]:
        emit('[CHANGED] ' + safe_text(line))
    emit(f'[RESULT] PASS · bootstrap.sh exit=0 · Artifact Version={artifact_version} · lock SHA256 valid · changed files={len(changed)}')
    emit('[NOTE] bootstrap.sh 成功不代表 Jenkins 或 GMX Upload 已执行')
    return {'verdict':'PASS','kind':'bootstrap','branch':branch,'sha':sha,
            'artifact_version':artifact_version, 'lock_hashes':checks,
            'changed':changed[:100], 'work_dir':str(repo), 'output_lines':len(lines)}


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

