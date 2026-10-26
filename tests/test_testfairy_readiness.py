"""Browser readiness regressions using intercepted offline pages only."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from playwright.sync_api import sync_playwright
from appbuilder.testfairy import TestFairyBrowser
from appbuilder.jenkins import NeedsAttention

BASE='https://hsbc-mobilex.testfairy.com'

class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=sync_playwright().start()
        cls.browser=cls.engine.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.engine.stop()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.worker=TestFairyBrowser({'testfairy':{'wait_seconds':2}},Path(self.tmp.name),{'login_url':BASE+'/login'},login_credentials=('offline@example.com','PRIVATE_FIXTURE_PASSWORD'),visible=False)
        self.worker.page=self.context.new_page();self.worker.context=self.context
        self.pages={};self.requests=[]
        def route(r):
            self.requests.append((r.request.url.removeprefix(BASE),r.request.method))
            r.fulfill(content_type='text/html',body=self.pages.get(r.request.url.removeprefix(BASE),'<main>Offline fixture</main>'))
        self.context.route('**/*',route)
    def test_delayed_login_form_submits_once_after_visible_ready(self):
        form='<form method="post" action="/authenticate"><input type="email" name="email"><input type="password" name="password"><button type="submit">Log in</button></form>'
        self.pages['/login']='<main id="form"></main><script>setTimeout(()=>document.querySelector("#form").innerHTML='+json.dumps(form)+',250)</script>'
        self.pages['/authenticate']='<script>location.replace("/my")</script>'
        self.pages['/my']='<h1>APPS</h1>'
        self.worker.login({'path':'IOS_Upload'},deadline=time.monotonic()+4)
        self.assertEqual(self.requests.count(('/authenticate','POST')),1)
        self.assertEqual(self.worker.page.url,BASE+'/my')
        self.assertNotIn('PRIVATE_FIXTURE_PASSWORD',Path(self.worker.trace.summary['log_path']).read_text())
    def test_delayed_login_form_with_external_action_never_submits(self):
        form='<form action="https://unapproved.example/submit"><input type="email"><input type="password"><button>Log in</button></form>'
        self.pages['/login']='<main id="form"></main><script>setTimeout(()=>document.querySelector("#form").innerHTML='+json.dumps(form)+',100)</script>'
        with self.assertRaisesRegex(NeedsAttention,'不同源'):
            self.worker.login({'path':'IOS_Upload'},deadline=time.monotonic()+2)
        self.assertEqual(self.worker.page.locator('input[type=password]').input_value(),'')
        self.assertFalse(any(method=='POST' for _,method in self.requests))
    def test_noop_folder_click_does_not_advance_to_already_visible_flat_child(self):
        self.pages['/my']='<button>IOS_Upload</button><button onclick="window.wrong=true">IOS_Develop</button>'
        with self.assertRaises(NeedsAttention):
            self.worker._folder('IOS_Upload/IOS_Develop',deadline=time.monotonic()+.6)
        self.assertIsNone(self.worker.page.evaluate('window.wrong'))
    def test_same_origin_url_change_without_expected_child_is_not_success(self):
        self.pages['/my']='<a href="/wrong">IOS_Upload</a>'
        self.pages['/wrong']='<h1>Unrelated page</h1>'
        with self.assertRaisesRegex(NeedsAttention,'未证实'):
            self.worker._folder('IOS_Upload/IOS_Develop',deadline=time.monotonic()+.6)
    def test_delayed_exact_breadcrumb_is_required_for_no_navigation_click(self):
        self.pages['/my']='''<button onclick="setTimeout(()=>document.querySelector('#crumb').innerHTML='<span>IOS_Upload</span>',200)">IOS_Upload</button><nav id="crumb" class="breadcrumb"></nav>'''
        started=time.monotonic()
        self.worker._folder('IOS_Upload',deadline=started+2)
        self.assertGreaterEqual(time.monotonic()-started,.2)
        self.assertEqual(self.worker.page.url,BASE+'/my')
    def test_wrong_breadcrumb_does_not_prove_requested_path(self):
        self.pages['/my']='''<button onclick="document.querySelector('#crumb').innerHTML='<span>Other</span>'">IOS_Upload</button><nav id="crumb" class="breadcrumb"></nav>'''
        with self.assertRaisesRegex(NeedsAttention,'未证实'):
            self.worker._folder('IOS_Upload',deadline=time.monotonic()+.6)
    def test_same_page_redraw_to_new_expected_child_then_exact_path(self):
        child='<button onclick="document.querySelector(\'#crumb\').innerHTML=\'<span>IOS_Upload</span><span>IOS_Develop</span>\'">IOS_Develop</button>'
        self.pages['/my']='<div id="list"><button id="root">IOS_Upload</button></div><nav id="crumb" class="breadcrumb"></nav><script>document.querySelector("#root").addEventListener("click",()=>setTimeout(()=>document.querySelector("#list").innerHTML='+json.dumps(child)+',150))</script>'
        self.worker._folder('IOS_Upload/IOS_Develop',deadline=time.monotonic()+2)
        self.assertEqual(self.worker.page.locator('#crumb').inner_text(),'IOS_UploadIOS_Develop')
    def test_explicit_wrong_current_breadcrumb_blocks_new_flat_child(self):
        from appbuilder.testfairy import _FOLDER_SELECTOR
        self.pages['/my']='<nav aria-label="breadcrumb"><span>APPS</span><span aria-current="page">WrongRoot</span></nav><a href="/child">IOS_Develop</a>'
        self.worker.page.goto(BASE+'/my');self.worker._observe_loading()
        with self.assertRaisesRegex(NeedsAttention,'面包屑.*冲突'):
            self.worker._await_directory_transition(['IOS_Upload','IOS_Develop'],0,_FOLDER_SELECTOR,{'nodes':set()},time.monotonic()+.5)
        self.assertEqual(self.worker.page.url,BASE+'/my')
    def test_old_current_breadcrumb_waits_until_exact_target_path(self):
        from appbuilder.testfairy import _FOLDER_SELECTOR
        self.pages['/my']='<nav aria-label="breadcrumb"><span>APPS</span><span id="current" aria-current="page">WrongRoot</span></nav><a href="/child">IOS_Develop</a><script>setTimeout(()=>document.querySelector("#current").textContent="IOS_Upload",200)</script>'
        self.worker.page.goto(BASE+'/my');self.worker._observe_loading();started=time.monotonic()
        proof=self.worker._await_directory_transition(['IOS_Upload','IOS_Develop'],0,_FOLDER_SELECTOR,{'nodes':set()},started+2)
        self.assertEqual(proof,'exact_path_selected')
        self.assertGreater(time.monotonic()-started,.1)
