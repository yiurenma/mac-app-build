"""Offline Chromium reproduction of expired cached SSO and Jenkins error shells."""
import copy
import io
from contextlib import redirect_stdout
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
from appbuilder.jenkins import JenkinsBrowser, NeedsAttention, page_error_kind, has_login_inputs
from appbuilder.runner import effective_task_config, log_jenkins_targets
from appbuilder.versions import job_urls

HOST='https://jenkins.corp.hsbc'
PATH='/job/release-build/job/release_1_3_202610111410/'
TARGET=HOST+PATH
SHELL='<title>Oops! - Jenkins</title><h1>Oops!</h1><h2>Not Found.</h2><p>This page may not exist, or you may not have permission to see it.</p>'
LINK='<a href="/login?from='+PATH+'">Sign in</a>'
JOB='<title>Release branch - Jenkins</title><h1>release_1_3_202610111410</h1><a href="build" onclick="window.builds=1;return false">Build Now</a>'
LOGIN='<title>Sign in - Jenkins</title><form action="/j_security_check" method="post"><input name="j_username"><input name="j_password" type="password"><button type="submit">Sign in</button></form>'


class CachedSessionRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=sync_playwright().start()
        cls.browser=cls.engine.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.engine.stop()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.auth=Path(self.tmp.name)/'plugin.json'
        seed=self.browser.new_context()
        seed.add_cookies([{'name':'session','value':'expired','url':HOST}])
        seed.storage_state(path=str(self.auth));seed.close()
        self.context=self.browser.new_context(storage_state=str(self.auth))
        self.addCleanup(self.context.close)
        self.requests=[]
        self.mode='expired'
        self.context.route('**/*',self.route)
        self.worker=object.__new__(JenkinsBrowser)
        self.worker.browser=Mock()
        def fresh_context(**kwargs):
            ctx=self.browser.new_context(**kwargs);ctx.route('**/*',self.route)
            self.addCleanup(ctx.close);return ctx
        self.worker.browser.new_context.side_effect=fresh_context
        self.worker.context=self.context;self.worker.page=self.context.new_page()
        self.worker.site='plugin';self.worker.credentials=('offline-user','offline-password')
        self.worker.visible=False;self.worker.manual_login=False
        self.worker.conf={'discover_timeout_minutes':.01,'auth_allowed_hosts':['*.hsbc'],'plugin_parent':HOST+'/job/release-build/job/'}
        self.worker.discover_poll=1;self.worker.auth_path=self.auth

    def route(self, route):
        path=urlsplit(route.request.url).path
        self.requests.append(path)
        if path=='/job/release-build/':
            route.fulfill(status=403 if self.mode=='parent-shell' else 200,content_type='text/html',body=SHELL if self.mode=='parent-shell' else LOGIN)
        elif path=='/login':
            route.fulfill(status=200,content_type='text/html',body=LOGIN)
        elif path=='/j_security_check':
            route.fulfill(status=200,headers={'set-cookie':'session=valid; Path=/; Secure'},content_type='text/html',body='<script>location.replace('+json.dumps(TARGET)+')</script>')
        elif path=='/sso/step':
            route.fulfill(status=200,content_type='text/html',body='<h1>Company SSO</h1><input name="username"><button>Next</button>')
        elif path=='/dashboard/':
            route.fulfill(status=200,content_type='text/html',body='<h1>Jenkins dashboard</h1>')
        elif path==PATH:
            authenticated='session=valid' in route.request.headers.get('cookie','')
            if self.mode=='wrong-target' and authenticated:
                route.fulfill(status=200,content_type='text/html',body='<script>location.replace('+json.dumps(HOST+'/dashboard/')+')</script>')
            elif self.mode=='valid' or (self.mode in ('expired','no-link-expired','parent-shell') and authenticated):
                route.fulfill(status=200,content_type='text/html',body=JOB)
            elif self.mode=='acl':
                route.fulfill(status=403,content_type='text/html',body='<h1>Access Denied</h1><p>alice is missing the Job/Read permission</p>'+LINK)
            elif self.mode=='external':
                route.fulfill(status=403,content_type='text/html',body=SHELL+'<a href="https://evil.example/login">Sign in</a>')
            else:
                status=200 if self.mode=='soft404' else 403
                link=LINK if self.mode in ('expired','still-denied') else ''
                route.fulfill(status=status,content_type='text/html',body=SHELL+link)
        else:
            route.fulfill(status=404,body='not found')

    def test_expired_cached_session_403_shell_recovers_original_job_once(self):
        output=io.StringIO()
        with redirect_stdout(output):
            self.worker.wait_job(TARGET)
        self.assertNotIn('offline-password',output.getvalue())
        self.assertNotIn('session=valid',output.getvalue())
        self.assertEqual(self.worker.page.url,TARGET)
        self.worker.browser.new_context.assert_called_once_with(accept_downloads=False)
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertEqual(self.requests.count('/j_security_check'),1)
        self.assertTrue(all(path in (PATH,'/login','/j_security_check','/job/release-build/') for path in self.requests))
        self.assertEqual(self.worker.page.evaluate('window.builds||0'),0)
        saved=json.loads(self.auth.read_text())
        self.assertTrue(any(c['name']=='session' and c['value']=='valid' for c in saved['cookies']))

    def test_403_shell_without_link_uses_saved_credentials_automatically(self):
        self.mode='no-link-expired'
        self.worker.wait_job(TARGET)
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertEqual(self.requests.count('/j_security_check'),1)
        self.assertEqual(self.worker.page.url,TARGET)
        self.assertEqual(self.worker.page.evaluate('window.builds||0'),0)
        self.assertTrue(any(c['value']=='valid' for c in json.loads(self.auth.read_text())['cookies']))

    def test_auto_login_continues_existing_bootstrap_without_new_build(self):
        from appbuilder.jenkins import ensure_bootstrap
        self.mode='no-link-expired'
        self.worker.request_build=Mock()
        self.worker.wait_result=Mock(return_value=('SUCCESS',''))
        self.worker.wait_parameter_mode=Mock()
        state={'runtime':{'plugin_bootstrap_number':7}}
        with patch('appbuilder.jenkins.read_state',return_value=state), patch('appbuilder.jenkins.patch_runtime') as save:
            ensure_bootstrap(self.worker,TARGET,'original-task','plugin')
        self.worker.request_build.assert_not_called()
        self.worker.wait_result.assert_called_once_with(TARGET,7)
        self.assertEqual(self.requests.count('/j_security_check'),1)
        save.assert_any_call('original-task',plugin_bootstrap_completed=True)
        self.assertEqual(state,{'runtime':{'plugin_bootstrap_number':7}})

    def test_no_link_recovery_still_denied_is_attempted_only_once(self):
        self.mode='ambiguous';before=self.auth.read_bytes()
        for _ in range(2):
            with self.assertRaisesRegex(NeedsAttention,'HTTP 403'):
                self.worker.wait_job(TARGET)
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertEqual(self.requests.count('/j_security_check'),1)
        self.assertEqual(self.auth.read_bytes(),before)

    def test_parent_without_controls_uses_standard_same_origin_login(self):
        self.mode='parent-shell'
        self.worker.wait_job(TARGET)
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertEqual(self.requests.count('/login'),1)
        self.assertEqual(self.requests.count('/j_security_check'),1)

    def test_failed_fresh_auth_preserves_previous_cache_and_context(self):
        self.mode='no-link-expired';before=self.auth.read_bytes()
        old_context,old_page=self.worker.context,self.worker.page
        with patch.object(self.worker,'_login_if_needed',side_effect=NeedsAttention('additional verification')):
            with self.assertRaisesRegex(NeedsAttention,'additional verification'):
                self.worker.wait_job(TARGET)
        self.assertEqual(self.auth.read_bytes(),before)
        self.assertIs(self.worker.context,old_context)
        self.assertIs(self.worker.page,old_page)
        self.assertEqual(self.requests.count('/j_security_check'),0)

    def test_same_host_wrong_target_200_does_not_cache_or_continue(self):
        self.mode='wrong-target';before=self.auth.read_bytes()
        with self.assertRaisesRegex(NeedsAttention,'精确原目标'):
            self.worker.wait_job(TARGET)
        self.assertEqual(self.auth.read_bytes(),before)
        self.assertEqual(self.requests.count('/j_security_check'),1)

    def test_target_identity_preserves_encoded_gmx_branch_slashes(self):
        from appbuilder.jenkins import same_target_url
        target=HOST+'/job/upload%2FABC-123/'
        self.assertTrue(same_target_url(target.rstrip('/'),target))
        for other in (HOST+'/job/upload/ABC-123/',HOST+'/job/upload%252FABC-123/',
                      HOST+'/job/other/',target+'?different=1'):
            self.assertFalse(same_target_url(other,target))

    def test_no_saved_credentials_does_not_invent_auto_login(self):
        self.mode='ambiguous';self.worker.credentials=None
        with self.assertRaises(NeedsAttention):self.worker.wait_job(TARGET)
        self.assertEqual(self.requests,[PATH])

    def test_configured_login_origin_and_context_are_checked(self):
        from appbuilder.jenkins import configured_login_entry
        self.assertEqual(configured_login_entry({'plugin_parent':HOST+'/jenkins/job/root/job/'},
            'plugin',HOST+'/jenkins/job/root/job/branch/'),HOST+'/jenkins/login')
        for parent in ('https://elsewhere.hsbc/job/root/job/',HOST+'/other/job/root/job/',
                       'http://jenkins.corp.hsbc/job/root/job/'):
            self.assertIsNone(configured_login_entry({'plugin_parent':parent},'plugin',TARGET))

    def test_three_runs_with_expiring_saved_sessions_each_recover_once(self):
        self.mode='no-link-expired'
        for run in range(3):
            if run:
                cached=json.loads(self.auth.read_text())
                for cookie in cached['cookies']:
                    if cookie['name']=='session':cookie['value']='expired'
                self.auth.write_text(json.dumps(cached))
                context=self.browser.new_context(storage_state=str(self.auth))
                self.addCleanup(context.close);context.route('**/*',self.route)
                worker=copy.copy(self.worker);worker.context=context;worker.page=context.new_page()
                worker._login_recovery_targets=set()
            else:worker=self.worker
            worker.wait_job(TARGET)
            self.assertEqual(worker.page.url,TARGET)
            self.assertEqual(self.requests.count('/job/release-build/'),run+1)
            self.assertEqual(worker.page.evaluate('window.builds||0'),0)

    def test_gmx_three_expired_sessions_use_gmx_parent_and_separate_cache(self):
        plugin_cache=self.auth;before=plugin_cache.read_bytes()
        self.auth=Path(self.tmp.name)/'gmx.json';self.auth.write_bytes(before)
        self.worker.site='gmx';self.worker.auth_path=self.auth
        self.worker.conf['gmx_parent']=self.worker.conf.pop('plugin_parent')
        self.test_three_runs_with_expiring_saved_sessions_each_recover_once()
        self.assertEqual(plugin_cache.read_bytes(),before)

    def test_gmx_submitted_baseline_recovers_and_detects_without_reclick(self):
        from appbuilder.jenkins import formal_build
        self.mode='no-link-expired';self.worker.site='gmx'
        self.worker.conf['gmx_parent']=self.worker.conf.pop('plugin_parent')
        self.worker.auth_path=Path(self.tmp.name)/'gmx.json'
        self.worker.request_build=Mock()
        self.worker.build_numbers=Mock(return_value={1,7})
        self.worker.wait_result=Mock(return_value=('SUCCESS','Finished: SUCCESS'))
        state={'runtime':{'gmx_formal_baseline':[1]}}
        with patch('appbuilder.jenkins.read_state',return_value=state), patch('appbuilder.jenkins.patch_runtime') as save:
            number,_=formal_build(self.worker,TARGET,'original-gmx','gmx')
        self.assertEqual(number,7)
        self.worker.request_build.assert_not_called()
        self.worker.wait_result.assert_called_once_with(TARGET,7)
        save.assert_any_call('original-gmx',gmx_formal_number=7)
        self.assertEqual(state,{'runtime':{'gmx_formal_baseline':[1]}})

    def test_explicit_acl_denial_does_not_login_or_retry(self):
        self.mode='acl';before=self.auth.read_bytes()
        with self.assertRaisesRegex(NeedsAttention,'明确报告缺少'):
            self.worker.wait_job(TARGET)
        self.assertEqual(self.requests,[PATH]);self.assertEqual(self.auth.read_bytes(),before)

    def test_403_not_found_without_login_evidence_remains_ambiguous_and_stops(self):
        self.mode='ambiguous'
        with self.assertRaisesRegex(NeedsAttention,'不能仅凭') as error:
            self.worker.wait_job(TARGET)
        self.assertIn('HTTP 403',str(error.exception))
        self.assertIn(TARGET,str(error.exception))
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertEqual(self.requests.count('/j_security_check'),1)

    def test_200_soft_not_found_is_not_a_ready_job(self):
        self.mode='soft404';self.worker.conf['discover_timeout_minutes']=.01
        with self.assertRaisesRegex(TimeoutError,'最后 HTTP=200'):
            self.worker.wait_job(TARGET)
        self.assertEqual(page_error_kind(self.worker.page),'not-found')
        self.assertNotIn('/j_security_check',self.requests)

    def test_login_that_still_returns_403_is_bounded(self):
        self.mode='still-denied'
        for _ in range(2):
            with self.assertRaisesRegex(NeedsAttention,'HTTP 403'):
                self.worker.wait_job(TARGET)
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertEqual(self.requests.count('/j_security_check'),1)

    def test_external_sign_in_link_is_not_followed(self):
        self.mode='external'
        with self.assertRaises(NeedsAttention):self.worker.wait_job(TARGET)
        self.assertEqual(self.requests.count('/job/release-build/'),1)
        self.assertNotIn('evil.example',self.worker.page.url)

    def test_valid_session_is_reused_without_login_or_cache_rewrite(self):
        self.mode='valid';before=self.auth.read_bytes()
        self.worker.wait_job(TARGET)
        self.assertEqual(self.requests,[PATH]);self.assertEqual(self.auth.read_bytes(),before)

    def test_same_host_username_only_sso_does_not_cache_unfinished_login(self):
        self.worker.page.goto(HOST+'/sso/step')
        self.worker.visible=True;self.worker.manual_login=True;self.worker.credentials=None
        self.assertTrue(has_login_inputs(self.worker.page))
        # Use a static page for the fake-clock timeout: changing the global
        # clock underneath Playwright itself would also freeze its event loop.
        from types import SimpleNamespace
        self.worker.page=SimpleNamespace(url=HOST+'/sso/step', title=lambda:'Company SSO',
            locator=lambda css:SimpleNamespace(count=lambda:1 if 'username' in css else 0,all_text_contents=lambda:[]))
        cached=Mock();self.worker._cache_login=cached
        before=self.auth.read_bytes()
        times=iter([0,0,181])
        with patch('appbuilder.jenkins.time.monotonic',side_effect=lambda:next(times,181)),patch('appbuilder.jenkins.time.sleep'):
            with self.assertRaisesRegex(NeedsAttention,'180 秒'):
                self.worker._login_if_needed(TARGET)
        cached.assert_not_called();self.assertEqual(self.auth.read_bytes(),before)


class SavedTargetTests(unittest.TestCase):
    def test_screenshot_path_mapping_and_resume_snapshot_are_preserved(self):
        from appbuilder.config import load_config
        config=load_config()
        # Historical release snapshot, independent of the current feature default.
        config['jenkins']['plugin_parent']=config['jenkins']['plugin_parent'].replace('/feature-build/', '/release-build/')
        plugin='release/1.3.202610111410';gmx='upload/TRANSACT-667788-auto202610111410'
        urls=job_urls(config,plugin,gmx)
        self.assertTrue(urls['plugin_job'].endswith('/job/release-build/job/release_1_3_202610111410/'))
        params={'plugin_branch':plugin,'gmx_branch':gmx,**urls,
                'plugin_source':'feature/TRANSACT-122555','gmx_source':'upload/TRANSACT-667788',
                'jenkins_settings':{'plugin_parent':config['jenkins']['plugin_parent']}}
        original=copy.deepcopy(params)
        config['jenkins']['plugin_parent']='https://other.hsbc/job/new/job/'
        effective=effective_task_config(config,params)
        self.assertEqual(job_urls(effective,plugin,gmx)['plugin_job'],urls['plugin_job'])
        with patch('appbuilder.runner.log') as log:
            log_jenkins_targets(params,effective)
            self.assertIn(urls['plugin_job'],str(log.call_args_list))
        self.assertEqual(params,original)
        sensitive=copy.deepcopy(params);sensitive['plugin_job']+='?token=DO_NOT_LOG'
        with patch('appbuilder.runner.log') as log:
            log_jenkins_targets(sensitive,effective)
            self.assertNotIn('DO_NOT_LOG',str(log.call_args_list))
        # A legacy snapshot without settings still keeps its saved URL.
        params.pop('jenkins_settings');original=copy.deepcopy(params)
        with patch('appbuilder.runner.log') as log:
            log_jenkins_targets(params,config)
            self.assertIn('不自动改写',str(log.call_args_list))
        self.assertEqual(params,original)


class ConsoleRecoveryTests(unittest.TestCase):
    def test_both_sites_retry_only_exact_read_after_session_recovery(self):
        from types import SimpleNamespace
        from appbuilder.jenkins import read_full_console
        for site in ('plugin','gmx'):
            with self.subTest(site=site):
                url=HOST+'/job/'+site+'/7/consoleText'
                denied=SimpleNamespace(status=403,url=url,headers={'content-type':'text/html'},dispose=Mock())
                success=SimpleNamespace(status=200,url=url,headers={'content-type':'text/plain'},text=lambda:'Finished: SUCCESS',dispose=Mock())
                old=SimpleNamespace(request=SimpleNamespace(get=Mock(return_value=denied)))
                fresh=SimpleNamespace(request=SimpleNamespace(get=Mock(return_value=success)))
                worker=SimpleNamespace(context=old,conf={},site=site)
                def recover(target,timeout):
                    self.assertEqual(target,url);worker.context=fresh;return 200
                worker.visit=Mock(side_effect=recover)
                self.assertEqual(read_full_console(worker,HOST+'/job/'+site+'/',7),'Finished: SUCCESS')
                worker.visit.assert_called_once()
                old.request.get.assert_called_once_with(url,timeout=300000)
                fresh.request.get.assert_called_once_with(url,timeout=300000)

    def test_denial_after_console_recovery_stops_without_second_request(self):
        from types import SimpleNamespace
        from appbuilder.jenkins import authenticated_console_response
        response=SimpleNamespace(status=403,url=TARGET,headers={},dispose=Mock())
        get=Mock(return_value=response)
        worker=SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=get)),visit=Mock(return_value=403))
        with self.assertRaises(NeedsAttention):authenticated_console_response(worker,TARGET,10000)
        self.assertEqual(get.call_count,1)
