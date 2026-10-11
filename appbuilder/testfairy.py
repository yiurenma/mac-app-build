"""Read-only TestFairy matching for the exact completed GMX build."""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin, parse_qsl, urlunsplit
from playwright.sync_api import sync_playwright
from .jenkins import NeedsAttention, browser_launch_options
from .auth import submit_recognized_login

DEFAULT_LOGIN = 'https://hsbc-mobilex.testfairy.com/login'


def origin(login):
    u = urlsplit(login)
    if (u.scheme != 'https' or not u.hostname or not u.hostname.endswith('.testfairy.com')
            or u.username or u.password or u.port not in (None,443)
            or u.query or u.fragment or u.path.rstrip('/') != '/login'):
        raise ValueError('TestFairy 登录地址必须是租户的 HTTPS /login，无账号密码或查询参数')
    return 'https://' + u.netloc


def approved_target(login, config):
    base=origin(login)
    allowed=config.get('testfairy',{}).get('allowed_hosts',['hsbc-mobilex.testfairy.com'])
    if urlsplit(base).hostname not in allowed:
        raise ValueError('TestFairy 目标未列入本机配置允许名单，禁止提交公司凭据')
    return base


def saved_auth_available(login, data):
    base=origin(login);host=urlsplit(base).hostname
    if (Path(data)/'auth'/('testfairy-'+host+'.json')).is_file():return True
    if sys.platform!='darwin':return False
    service='mac-app-builder.testfairy.'+host
    # Existence checks only: do not read password values to validate a schedule.
    for item in (service,service+'.account'):
        result=subprocess.run(['security','find-generic-password','-s',item],capture_output=True,timeout=10)
        if result.returncode:return False
    return True


def credentials(login, value=None):
    """Isolated opt-in Keychain entry. Never copy credentials into task metadata."""
    host = urlsplit(origin(login)).hostname
    service = 'mac-app-builder.testfairy.' + host
    if sys.platform != 'darwin':
        if value is not None:
            raise ValueError('TestFairy 保存凭据仅支持 Mac Keychain')
        return None
    def command(args):
        return subprocess.run(['security', *args],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if value is not None:
        user,password=value
        a=command(['add-generic-password','-U','-s',service,'-a',user,'-w',password])
        if a.returncode:raise ValueError('TestFairy Keychain 保存失败，请检查本机权限')
        b=command(['add-generic-password','-U','-s',service+'.account','-a','default','-w',user])
        if b.returncode:raise ValueError('TestFairy Keychain 账号索引保存失败')
        return None
    a=command(['find-generic-password','-s',service+'.account','-a','default','-w'])
    if a.returncode or not a.stdout.strip():return None
    user=a.stdout.strip()
    b=command(['find-generic-password','-s',service,'-a',user,'-w'])
    return (user,b.stdout.rstrip('\n')) if not b.returncode and b.stdout.rstrip('\n') else None


def settings(body, config=None):
    enabled=body.get('testfairy_enabled',False)
    persist=body.get('testfairy_save_credentials',False)
    if not isinstance(enabled,bool) or not isinstance(persist,bool):raise ValueError('TestFairy 开关必须是布尔值')
    login=body.get('testfairy_login_url') or DEFAULT_LOGIN
    approved_target(login, config or {})
    path=body.get('testfairy_folder_path','').strip()
    if path and (len(path)>512 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. -]{0,127}',p) for p in path.split('/'))):
        raise ValueError('TestFairy 目录需使用逐层名称，以 / 分隔')
    return {'enabled':enabled,'login_url':login,'folder_path':path,'save_session':persist}


def expected_build(params, runtime):
    number=runtime.get('gmx_build_number') or runtime.get('gmx_formal_number')
    sha=runtime.get('gmx_sha','')
    if not isinstance(number,int) or isinstance(number,bool) or number<1 or not re.fullmatch(r'[0-9a-fA-F]{40}',sha):
        raise NeedsAttention('缺少当前 GMX Build 编号或完整提交 SHA，禁止猜测 TestFairy App')
    root=params['gmx_job'].rstrip('/')+'/'
    u=urlsplit(root)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise NeedsAttention('保存的 GMX Job URL 无效')
    gmx=params.get('gmx_parameters',{})
    folder=gmx.get('CUSTOM_FOLDER') or f"0_A_{params['country']}_{params['project']}"
    path=params.get('testfairy',{}).get('folder_path') or f"IOS_Upload/IOS_{gmx.get('BUILD_TYPE','Develop')}/{folder}"
    return {'build_url':root+str(number)+'/', 'number':number,'sha':sha.lower(),'folder':folder,'path':path}


def parse_candidate(text, expected):
    """All identity evidence must occur in the same App card/release notes."""
    urls=re.findall(r'https://[^\s<>"\']+',text)
    target=expected['build_url'].rstrip('/')
    if target not in [x.rstrip('/.,;)') for x in urls]:return None
    def field(label):
        m=re.search(r'(?im)^\s*'+label+r'\s*[:：=]?\s*([^\n\r]+)',text)
        return m.group(1).strip() if m else ''
    if field(r'Build\s*number')!=str(expected['number']):return None
    if field(r'(?:Commit\s*SHA|CommitSHA)') .lower()!=expected['sha']:return None
    if field(r'Folder\s*Param')!=expected['folder']:return None
    versions=set(re.findall(r'(?<![\w.])(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\s*\((\d+)\)',text))
    if len(versions)!=1:raise NeedsAttention('匹配的 TestFairy App 版本缺失或不唯一，不能生成邮件')
    version,build=next(iter(versions))
    return {'app_version':version,'app_build':build,**expected}


def share_url(raw, base):
    u=urlsplit(urljoin(base,raw));expected=urlsplit(base)
    if (u.scheme.lower()!='https' or (u.hostname or '').lower()!=(expected.hostname or '').lower()
            or u.port not in (None,443) or u.fragment or u.username or u.password
            or not re.fullmatch(r'/join/[A-Za-z0-9_-]+/?',u.path)):
        raise NeedsAttention('TestFairy 分享地址不是当前租户已有的 /join/ 链接')
    if any(re.search(r'token|password|secret|session|auth|key',k,re.I) for k,v in parse_qsl(u.query)):
        raise NeedsAttention('分享链接包含敏感认证查询参数，拒绝保存或展示')
    # Only normalize scheme/host/default port; path and query remain distinct.
    return urlunsplit(('https',u.hostname.lower(),u.path,u.query,''))


class TestFairyBrowser:
    def __init__(self, config, data, options, *, login_credentials=None, visible=True):
        self.config=config;self.options=options;self.base=approved_target(options['login_url'],config)
        self.auth_path=Path(data)/'auth'/('testfairy-'+urlsplit(self.base).hostname+'.json')
        self.login_credentials=login_credentials or credentials(options['login_url'])
        self.visible=visible
    def __enter__(self):
        self.engine=sync_playwright().start()
        try:
            self.browser=self.engine.chromium.launch(**browser_launch_options(self.config,headless=not self.visible))
            args={'accept_downloads':False}
            if self.options.get('save_session') and self.auth_path.exists():args['storage_state']=str(self.auth_path)
            self.context=self.browser.new_context(**args);self.page=self.context.new_page()
            self.page.set_default_timeout(12000)
            return self
        except Exception:
            if hasattr(self,'browser'):self.browser.close()
            self.engine.stop();raise
    def __exit__(self,*args):
        self.context.close();self.browser.close();self.engine.stop()
    def _same_origin(self):
        return urlsplit(self.page.url).netloc==urlsplit(self.base).netloc and urlsplit(self.page.url).scheme=='https'
    def login(self, expected):
        if self.options.get('save_session'):
            self.page.goto(self.base+'/my',wait_until='domcontentloaded')
            if (self._same_origin() and not self.page.locator('input[type=password]').count()
                    and self.page.get_by_text(expected['path'].split('/')[0],exact=True).count()):
                return
        self.page.goto(self.options['login_url'],wait_until='domcontentloaded')
        if not self._same_origin():raise NeedsAttention('TestFairy 登录跳转离开允许租户，未自动填写凭据')
        if self._same_origin() and self.page.locator('input[type=password]').count():
            if self.login_credentials:
                # Submit only same-origin TestFairy forms, never an IdP's password form.
                forms=self.page.locator('form:has(input[type=password])')
                if forms.count()!=1:raise NeedsAttention('TestFairy 登录表单不唯一')
                action=urljoin(self.page.url,forms.get_attribute('action') or self.page.url)
                if urlsplit(action).netloc!=urlsplit(self.base).netloc or urlsplit(action).scheme!='https':
                    raise NeedsAttention('TestFairy 登录提交地址不同源，停止自动填写')
                try:
                    submitted=submit_recognized_login(self.page,*self.login_credentials)
                except Exception:
                    raise NeedsAttention('TestFairy 登录填写或提交失败，凭据不记录到日志') from None
                if not submitted:
                    raise NeedsAttention('TestFairy 登录控件无法安全识别')
        deadline=time.monotonic()+(180 if self.visible else 25)
        while time.monotonic()<deadline:
            if not self._same_origin():raise NeedsAttention('TestFairy 认证跳转离开允许租户，已停止')
            if (self._same_origin() and urlsplit(self.page.url).path.rstrip('/')!='/login'
                    and not self.page.locator('input[type=password]').count()):
                self.page.goto(self.base+'/my',wait_until='domcontentloaded')
                if (self._same_origin() and not self.page.locator('input[type=password]').count()
                        and (self.page.get_by_text('APPS',exact=True).count()
                             or self.page.get_by_text(expected['path'].split('/')[0],exact=True).count())):
                    return
            self.page.wait_for_timeout(250)
        raise NeedsAttention('TestFairy 登录未完成；请在可见 Chrome 完成 MFA/额外验证，不会自动绕过')
    def _save_session(self):
        if not self.options.get('save_session'):return
        self.auth_path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=self.auth_path.parent,prefix='.testfairy-');os.close(fd)
        try:
            self.context.storage_state(path=name);os.chmod(name,0o600);os.replace(name,self.auth_path)
        finally:
            if os.path.exists(name):os.unlink(name)
    def _folder(self,path):
        self.page.goto(self.base+'/my',wait_until='domcontentloaded')
        if not self._same_origin() or self.page.locator('input[type=password]').count():
            raise NeedsAttention('TestFairy 会话已失效，停止查找，构建不会重跑')
        for label in path.split('/'):
            item=self.page.get_by_text(label,exact=True)
            if item.count()!=1:raise NeedsAttention('TestFairy 目录无法唯一定位：'+label+'；请核对实际目录或配置 folder_path')
            item.click()
            if not self._same_origin():raise NeedsAttention('TestFairy 目录跳转离开当前租户')
    def find(self,expected):
        self.login(expected)
        cfg=self.config.get('testfairy',{})
        deadline=time.monotonic()+max(1,min(1800,float(cfg.get('wait_seconds',600))))
        selector=cfg.get('card_selector','article, .app-card, .build-card, .version-card, [data-build-id]')
        while True:
            self._folder(expected['path'])
            cards=self.page.locator(selector)
            matches=[]
            for i in range(min(cards.count(),100)):
                card=cards.nth(i);parsed=parse_candidate(card.inner_text(),expected)
                if parsed:matches.append((card,parsed))
            if len(matches)>1:raise NeedsAttention('同一 GMX 构建匹配多个 TestFairy App，停止生成链接/邮件')
            if matches:
                card,parsed=matches[0]
                links=card.locator('a[href*="/join/"]')
                if not links.count():
                    # Only open the existing Share view. Never Enable/Create/Save permissions.
                    share=card.get_by_role('button',name=re.compile(r'^Share$',re.I))
                    if share.count()!=1:share=card.get_by_role('link',name=re.compile(r'^Share$',re.I))
                    if share.count()!=1:raise NeedsAttention('匹配 App 的现有 Share 入口无法唯一识别')
                    share.click()
                    if not self._same_origin():raise NeedsAttention('Share 页面离开 TestFairy 当前租户')
                    dialog=self.page.get_by_role('dialog')
                    scope=dialog if dialog.count()==1 else self.page
                    links=scope.locator('a[href*="/join/"]')
                    raw=[links.nth(i).get_attribute('href') for i in range(links.count())]
                    raw += scope.locator('input[value*="/join/"]').evaluate_all('(xs)=>xs.map(x=>x.value)')
                else:raw=[links.nth(i).get_attribute('href') for i in range(links.count())]
                urls={share_url(value,self.base) for value in raw if value}
                if len(urls)!=1:raise NeedsAttention('未找到唯一已有分享链接；不会创建公开权限')
                resolved=urls.pop()
                if urlsplit(resolved).query:
                    raise NeedsAttention('实际分享链接含查询参数，需人工核对；不会删改参数后发送另一地址')
                parsed.update(share_url=resolved,matched_at=datetime.now(timezone.utc).isoformat())
                self._save_session();return parsed
            if time.monotonic()>=deadline:raise NeedsAttention('等待本次 GMX 对应 TestFairy App 超时；未取最新包，未生成邮件')
            time.sleep(min(10,max(0,deadline-time.monotonic())))


def mail_preview(params,runtime,created):
    app=runtime['testfairy_app']
    subject=f"[{params['country']}] {params['project']} {app['app_version']} ({app['app_build']})"
    rows=[('App 分享链接',app['share_url']),('App 版本',app['app_version']+' ('+app['app_build']+')'),
          ('国家/地区',params['country']),('项目',params['project']),
          ('Plugin 来源分支',params['plugin_source']),('Plugin 构建分支',params['plugin_branch']),
          ('GMX 来源分支',params['gmx_source']),('GMX 构建分支',params['gmx_branch']),
          ('实际 Plugin 产物版本',runtime.get('artifact_version','')),
          ('Plugin Job',params['plugin_job']),('Plugin 正式构建编号',runtime.get('plugin_formal_number','')),
          ('Plugin 产物构建编号',runtime.get('plugin_artifact_number','')),
          ('GMX Build',app['build_url']),('GMX Build 编号',app['number']),
          ('GMX 提交 SHA',app['sha']),('Plugin 提交 SHA',runtime.get('plugin_sha','')),
          ('上传目录',app['path']),('任务创建时间',created),('链接匹配时间',app['matched_at'])]
    return {'status':'PREVIEW_ONLY','subject':subject,'body':'\n'.join(f'{key}: {value}' for key,value in rows),
            'to':params.get('outlook',{}).get('to',[]),'account':params.get('outlook',{}).get('account',''),'mode':'preview','task_id':params.get('id','')}
