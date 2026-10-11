import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
from playwright.sync_api import sync_playwright
from appbuilder.testfairy import (origin,settings,expected_build,parse_candidate,share_url,mail_preview,TestFairyBrowser)
from appbuilder.jenkins import NeedsAttention

BASE='https://hsbc-mobilex.testfairy.com'
JOB='https://gmx.corp.hsbc/job/release/job/upload%2FABC-1/'
SHA='a'*40
PARAMS={'id':'original-task','gmx_job':JOB,'country':'HK','project':'Payments',
        'gmx_parameters':{'CUSTOM_FOLDER':'custom_HK','BUILD_TYPE':'Release'},'testfairy':{},
        'plugin_source':'develop','plugin_branch':'release/1.3.2026','gmx_source':'develop','gmx_branch':'upload/ABC-1',
        'plugin_job':'https://plugin.corp.hsbc/job/plugin/'}
RUNTIME={'gmx_build_number':7,'gmx_sha':SHA,'artifact_version':'1.3.2026.7','plugin_formal_number':2,'plugin_artifact_number':3}
def note(number=7,sha=SHA,folder='custom_HK',version='3.76.0 (130363)'):
    return f'{version}\nBuild number: {number}\nBuild URL: {JOB}{number}/\nFolder Param: {folder}\nCommitSHA: {sha}'

class MetadataTests(unittest.TestCase):
    def test_exact_identity_not_latest_version_or_plugin_version(self):
        expected=expected_build(PARAMS,RUNTIME)
        self.assertEqual(expected['path'],'IOS_Upload/IOS_Release/custom_HK')
        self.assertIsNone(parse_candidate(note(number=8),expected))
        self.assertIsNone(parse_candidate(note(sha='b'*40),expected))
        self.assertIsNone(parse_candidate(note(folder='wrong'),expected))
        self.assertEqual(parse_candidate(note(),expected)['app_version'],'3.76.0')
        self.assertIsNone(parse_candidate(note().replace('%2F','/'),expected))
    def test_no_guess_without_saved_build_or_sha(self):
        for runtime in ({},{'gmx_build_number':True,'gmx_sha':SHA},{'gmx_build_number':7,'gmx_sha':'abc'}):
            with self.assertRaises(NeedsAttention):expected_build(PARAMS,runtime)
    def test_version_missing_or_multiple_fails(self):
        for content in (note(version='unknown'),note()+'\n1.2.3 (4)'):
            with self.assertRaises(NeedsAttention):parse_candidate(content,expected_build(PARAMS,RUNTIME))
    def test_existing_share_link_requires_exact_tenant_and_path(self):
        self.assertEqual(share_url('/join/abc123',BASE),BASE+'/join/abc123')
        for url in ('https://evil.testfairy.com/join/abc','/enable-sharing','/join/abc?token=secret'):
            with self.assertRaises(NeedsAttention):share_url(url,BASE)
    def test_preview_has_actual_versions_and_only_whitelisted_fields(self):
        app=parse_candidate(note(),expected_build(PARAMS,RUNTIME));app.update(share_url=BASE+'/join/current',matched_at='2026-10-11T00:00Z')
        runtime={**RUNTIME,'testfairy_app':app,'password':'SECRET'}
        preview=mail_preview({**PARAMS,'password':'SECRET'},runtime,'2026-10-10')
        text=json.dumps(preview)
        self.assertIn('3.76.0',text);self.assertIn('1.3.2026.7',text);self.assertNotIn('SECRET',text)
        self.assertEqual(preview['status'],'PREVIEW_ONLY');self.assertEqual(preview['to'],[])
    def test_feature_default_off_and_credentials_explicit_opt_in(self):
        self.assertEqual(settings({})['enabled'],False)
        self.assertFalse(settings({})['save_session'])
        self.assertTrue(settings({'testfairy_save_credentials':True})['save_session'])
        for url in ('http://hsbc-mobilex.testfairy.com/login','https://example.com/login',BASE+'/login?password=x'):
            with self.assertRaises(ValueError):origin(url)
    def test_shared_schedule_rejects_credentials(self):
        from appbuilder.schedules import validate_template
        for key in ('testfairy_user','testfairy_password'):
            with self.assertRaises(ValueError):validate_template({key:'SECRET'})

class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p=sync_playwright().start();cls.browser=cls.p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.p.stop()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.worker=TestFairyBrowser({'testfairy':{'wait_seconds':.01}},Path(self.tmp.name),
            {'login_url':BASE+'/login','save_session':False},login_credentials=('offline@example.com','FAKE_PASSWORD'),visible=False)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.worker.context=self.context;self.worker.page=self.context.new_page()
        self.requests=[];self.mode='valid'
        self.context.route('**/*',self.route)
    def route(self,route):
        path=route.request.url.split(BASE)[-1];self.requests.append((path,route.request.method))
        if path=='/login':
            route.fulfill(content_type='text/html',body='<form method="post" action="/login/"><input name="email" type="email"><input name="password" type="password"><input type="hidden" name="csrf-token" value="fixture"><button type="submit">Log in</button></form>')
        elif path=='/login/':
            self.assertIn('csrf-token=fixture',route.request.post_data)
            route.fulfill(content_type='text/html',body='<script>location.replace("/my")</script>')
        elif path=='/my':route.fulfill(content_type='text/html',body='<h1>APPS</h1><a href="/folder1">IOS_Upload</a>')
        elif path=='/folder1':route.fulfill(content_type='text/html',body='<a href="/folder2">IOS_Release</a>')
        elif path=='/folder2':route.fulfill(content_type='text/html',body='<a href="/folder3">custom_HK</a>')
        elif path=='/folder3':
            card='<article><pre>'+note()+'</pre><a href="/join/current">Share</a></article>'
            if self.mode=='multiple':card+=card
            if self.mode=='wrong':card='<article><pre>'+note(number=8)+'</pre><a href="/join/old">Share</a></article>'
            if self.mode=='permission':card='<article><pre>'+note()+'</pre><button>Enable public sharing</button></article>'
            route.fulfill(content_type='text/html',body=card)
        else:route.fulfill(status=404,body='Not Found')
    def fixture_page(self, html):
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html',body=html))

    def test_apps_entry_and_async_directory_ignore_hidden_and_heading_duplicates(self):
        self.context.unroute('**/*')
        def fixture(route):
            path=route.request.url.split(BASE)[-1]
            if path=='/my':
                html='''<div id="nav"></div><script>setTimeout(()=>document.querySelector('#nav').innerHTML='<a href="/apps">APPS</a>',250)</script>'''
            elif path=='/apps':
                html='''<h1>IOS_Upload</h1><a hidden href="/wrong">IOS_Upload</a><div id="list"></div>
                <script>setTimeout(()=>document.querySelector('#list').innerHTML='<a href="/folder1">IOS_Upload</a>',250)</script>'''
            else:return self.route(route)
            route.fulfill(content_type='text/html',body=html)
        self.context.route('**/*',fixture)
        result=self.worker.find(expected_build(PARAMS,RUNTIME))
        self.assertEqual(result['share_url'],BASE+'/join/current')
        self.assertTrue(self.worker.page.url.endswith('/folder3'))

    def test_final_cards_load_in_current_directory_without_resetting_my(self):
        self.context.unroute('**/*')
        self.worker.config['testfairy']['wait_seconds']=3
        visits=[]
        def fixture(route):
            path=route.request.url.split(BASE)[-1];visits.append(path)
            if path=='/folder1':
                html='''<div id="list"></div><script>setTimeout(()=>document.querySelector('#list').innerHTML='<a href="/folder2">IOS_Release</a>',250)</script>'''
            elif path=='/folder3':
                card='<article><pre>'+note()+'</pre><a href="/join/current">Share</a></article>'
                html='<div id="list"></div><script>setTimeout(()=>document.querySelector("#list").innerHTML='+json.dumps(card)+',1200)</script>'
            else:return self.route(route)
            route.fulfill(content_type='text/html',body=html)
        self.context.route('**/*',fixture)
        result=self.worker.find(expected_build(PARAMS,RUNTIME))
        self.assertEqual(result['share_url'],BASE+'/join/current')
        self.assertEqual(visits.count('/folder3'),1)
        self.assertNotIn('/my',visits[visits.index('/folder3')+1:])

    def share_fixture(self, onclick=''):
        self.context.unroute('**/*')
        def fixture(route):
            if route.request.url==BASE+'/folder3':
                html='<aside><a href="/join/unrelated-app">Other App</a></aside><article><pre>'+note()+"</pre><button onclick='"+onclick+"'>Share</button></article>"
                route.fulfill(content_type='text/html',body=html)
            else:self.route(route)
        self.context.route('**/*',fixture)

    def test_share_without_owned_container_never_uses_other_app_link(self):
        self.share_fixture()
        with self.assertRaisesRegex(NeedsAttention,'不会从整页'):
            self.worker.find(expected_build(PARAMS,RUNTIME))

    def test_share_waits_for_new_dialog_and_ignores_unrelated_page_link(self):
        dialog='<div role="dialog"><a href="/join/current">Share</a></div>'
        self.share_fixture('setTimeout(()=>document.body.insertAdjacentHTML("beforeend",'+json.dumps(dialog)+'),250)')
        self.assertEqual(self.worker.find(expected_build(PARAMS,RUNTIME))['share_url'],BASE+'/join/current')

    def test_current_breadcrumb_skips_ancestor_clicks(self):
        self.fixture_page('<nav aria-label="breadcrumb"><a>APPS</a><a>IOS_Upload</a><a>IOS_Release</a><span aria-current="page">custom_HK</span></nav><h1>custom_HK</h1>')
        self.worker._folder('IOS_Upload/IOS_Release/custom_HK')
        self.assertTrue(self.worker.page.url.endswith('/my'))

    def test_tree_scope_excludes_same_named_sibling_path(self):
        self.fixture_page('''<div role="tree"><div role="treeitem"><a onclick="">IOS_Upload</a><div role="group">
        <div role="treeitem"><a onclick="">IOS_Release</a><div role="group"><a onclick="window.chosen='right'">custom_HK</a></div></div>
        <div role="treeitem"><a onclick="">Other</a><div role="group"><a onclick="window.chosen='wrong'">custom_HK</a></div></div>
        </div></div></div>''')
        self.worker._folder('IOS_Upload/IOS_Release/custom_HK')
        self.assertEqual(self.worker.page.evaluate('window.chosen'),'right')

    def test_ambiguous_nodes_report_structure_without_click_or_secrets(self):
        self.fixture_page('<a href="/a?token=SECRET">IOS_Upload</a><a href="/b?token=SECRET">IOS_Upload</a>')
        with self.assertRaisesRegex(NeedsAttention,'可见交互候选 2') as error:
            self.worker._folder('IOS_Upload')
        self.assertNotIn('SECRET',str(error.exception))
        self.assertTrue(self.worker.page.url.endswith('/my'))

    def test_login_native_csrf_exact_card_and_existing_link(self):
        result=self.worker.find(expected_build(PARAMS,RUNTIME))
        self.assertEqual(result['share_url'],BASE+'/join/current')
        self.assertEqual(result['app_version'],'3.76.0')
        self.assertEqual([p for p,m in self.requests if m=='POST'],['/login/'])
        self.assertFalse(self.worker.auth_path.exists())
    def test_duplicate_matches_stop(self):
        self.mode='multiple'
        with self.assertRaisesRegex(NeedsAttention,'多个'):self.worker.find(expected_build(PARAMS,RUNTIME))
    def test_wrong_build_times_out_without_using_latest(self):
        self.mode='wrong'
        with self.assertRaisesRegex(NeedsAttention,'超时'):self.worker.find(expected_build(PARAMS,RUNTIME))
    def test_no_existing_share_does_not_enable_public_access(self):
        self.mode='permission'
        with self.assertRaisesRegex(NeedsAttention,'Share'):self.worker.find(expected_build(PARAMS,RUNTIME))
        self.assertFalse(any('enable' in p for p,m in self.requests))

class CredentialBoundaryTests(unittest.TestCase):
    def test_transient_secrets_go_only_to_worker_not_task_or_keychain(self):
        from appbuilder import web,cli
        body={'plugin_branch':'develop','gmx_branch':'develop','plugin_version':'1.3','country':'UK','project_name':'PayByBank',
              'testfairy_enabled':True,'testfairy_user':'private@example.com','testfairy_password':'TRANSIENT_SECRET'}
        row={'id':'202610101234-abcdef','created':'now','params':'{}','status':'QUEUED','pid':None,'stage':'INIT','note':''}
        with patch.object(cli,'doctor_check'),patch.object(web,'token_or_error'),patch.object(web,'active_count',return_value=0),patch.object(web,'create_task',return_value={'id':row['id']}) as create,patch.object(web,'task',return_value=row),patch.object(cli,'spawn') as spawn,patch('appbuilder.testfairy.credentials') as keychain:
            web.create_build(body)
        keychain.assert_not_called()
        self.assertNotIn('TRANSIENT_SECRET',json.dumps(create.call_args.args))
        self.assertNotIn('private@example.com',json.dumps(create.call_args.args))
        self.assertEqual(spawn.call_args.kwargs['transient_credentials']['testfairy'],['private@example.com','TRANSIENT_SECRET'])
    def test_opt_in_is_required_for_keychain_write(self):
        from appbuilder import web,cli
        body={'plugin_branch':'develop','gmx_branch':'develop','plugin_version':'1.3','country':'UK','project_name':'PayByBank',
              'testfairy_enabled':True,'testfairy_save_credentials':True,'testfairy_user':'private@example.com','testfairy_password':'TEST_SECRET'}
        row={'id':'202610101234-abcdef','created':'now','params':'{}','status':'QUEUED','pid':None,'stage':'INIT','note':''}
        with patch.object(cli,'doctor_check'),patch.object(web,'token_or_error'),patch.object(web,'active_count',return_value=0),patch.object(web,'create_task',return_value={'id':row['id']}),patch.object(web,'task',return_value=row),patch.object(cli,'spawn'),patch('appbuilder.testfairy.credentials') as keychain:
            web.create_build(body)
        keychain.assert_called_once_with(BASE+'/login',('private@example.com','TEST_SECRET'))

class PostBuildResumeTests(unittest.TestCase):
    def test_lookup_failure_then_resume_does_not_restart_successful_jenkins(self):
        from contextlib import nullcontext,ExitStack
        from appbuilder import runner
        from appbuilder.config import load_config
        params={**PARAMS,'plugin_version':'1.3','plugin_version_input':'1.3','visible':True,'verbose':False,
                'testfairy':{'enabled':True,'login_url':BASE+'/login','save_session':False}}
        state={'completed':['PLUGIN_CHECKOUT','PLUGIN_EDIT','PLUGIN_PUSH','PLUGIN_JENKINS','GMX_CHECKOUT','GMX_EDIT','GMX_LOCK','GMX_PUSH','GMX_JENKINS'],'runtime':copy.deepcopy(RUNTIME)}
        row={'id':'task','created':'today','status':'NEEDS_ATTENTION','params':json.dumps(params)}
        app=parse_candidate(note(),expected_build(params,RUNTIME));app.update(share_url=BASE+'/join/current',matched_at='today')
        browser=Mock();browser.__enter__=Mock(return_value=browser);browser.__exit__=Mock(return_value=False)
        browser.find.side_effect=[NeedsAttention('upload not ready'),app]
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            for name,value in [('task',lambda _:row),('read_state',lambda _:copy.deepcopy(state)),('task_folder',lambda _:Path(td)),('load_config',load_config),('token_or_error',lambda _:None),('credentials',lambda _:nullcontext({})),('repair_legacy_gmx_target',lambda *a:params),('complete',lambda _,name:state['completed'].append(name)),('patch_runtime',lambda _,**v:state['runtime'].update(v)),('update_task',lambda _,**v:row.update(v))]:
                stack.enter_context(patch.object(runner,name,side_effect=value))
            jenkins=stack.enter_context(patch.object(runner,'JenkinsBrowser'))
            checkout=stack.enter_context(patch.object(runner,'checkout'))
            stack.enter_context(patch('appbuilder.testfairy.TestFairyBrowser',return_value=browser))
            with self.assertRaisesRegex(NeedsAttention,'upload not ready'):runner._run_unlocked('task')
            self.assertEqual(row['status'],'NEEDS_ATTENTION')
            runner._run_unlocked('task')
            self.assertEqual(row['status'],'SUCCESS')
            self.assertEqual(state['runtime']['gmx_build_number'],7)
            jenkins.assert_not_called();checkout.assert_not_called()
            self.assertEqual(browser.find.call_count,2)
            self.assertIn('TESTFAIRY_LOOKUP',state['completed'])

class ScheduleAuthenticationTests(unittest.TestCase):
    def test_schedule_rejects_transient_only_authentication(self):
        from appbuilder.schedules import validate_template
        body={'testfairy_enabled':True}
        with patch('appbuilder.testfairy.saved_auth_available',return_value=True):
            with self.assertRaisesRegex(ValueError,'临时密码'):validate_template(body)
    def test_schedule_requires_existing_persistent_auth_even_if_checkbox_selected(self):
        from appbuilder.schedules import validate_template
        with patch('appbuilder.testfairy.saved_auth_available',return_value=False):
            with self.assertRaisesRegex(ValueError,'临时密码'):
                validate_template({'testfairy_enabled':True,'testfairy_save_credentials':True})
    def test_schedule_accepts_nonsecret_configuration_with_available_auth(self):
        from appbuilder.schedules import validate_template
        with patch('appbuilder.testfairy.saved_auth_available',return_value=True):
            result=validate_template({'testfairy_enabled':True,'testfairy_save_credentials':True})
        self.assertTrue(result['testfairy_enabled']);self.assertNotIn('testfairy_password',result)
