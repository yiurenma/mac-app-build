import json,os,tempfile,time,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
from appbuilder.testfairy import TestFairyBrowser,parse_candidate
from appbuilder.jenkins import NeedsAttention
from chromium_path import CHROMIUM
BASE='https://offline-share.testfairy.com'
EXPECTED={'path':'IOS_Upload','folder':'IOS_Upload','number':2,'sha':'b'*40,'build_url':'https://jenkins.fixture.invalid/job/upload/2/'}
META='3.76.0 (130425) Build number: 2 Build URL: https://jenkins.fixture.invalid/job/upload/2/ Folder Param: IOS_Upload CommitSHA: '+'b'*40
class ShareFixtures:
    headless=True
    @classmethod
    def setUpClass(cls):
        if not cls.headless and not os.environ.get('DISPLAY'):raise unittest.SkipTest('DISPLAY required')
        cls.engine=sync_playwright().start();cls.browser=cls.engine.chromium.launch(executable_path=CHROMIUM,headless=cls.headless,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.engine.stop()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.worker=TestFairyBrowser({'testfairy':{'allowed_hosts':['offline-share.testfairy.com']}},Path(self.tmp.name),{'login_url':BASE+'/login'},visible=not self.headless)
        self.page=self.context.new_page();self.worker.page=self.page;self.worker.context=self.context
        self.requests=[]
        def route(r):
            self.requests.append(r.request.method)
            if r.request.method!='GET':raise AssertionError('No writes')
            r.fulfill(content_type='text/html',body='<article><p>'+META+'</p></article>')
        self.context.route('**/*',route);self.page.goto(BASE+'/my')
        self.card=self.page.locator('article');self.parsed=parse_candidate(META,EXPECTED)
    def share(self,seconds=1):
        return self.worker._existing_share(self.card,self.parsed,EXPECTED,time.monotonic()+seconds)
    def later(self,js,delay=250):self.page.evaluate('(x)=>setTimeout(()=>eval(x.js),x.delay)',{'js':js,'delay':delay})
    def test_delayed_existing_share_is_read(self):
        self.later('document.querySelector("article").insertAdjacentHTML("beforeend",\'<a href="/join/right">Share</a>\')')
        self.assertEqual(self.share()['share_url'],BASE+'/join/right');self.assertEqual(self.requests,['GET'])
    def test_missing_share_is_bounded(self):
        start=time.monotonic()
        with self.assertRaises(NeedsAttention):self.share(.2)
        self.assertLess(time.monotonic()-start,1)
    def test_multiple_controls_never_clicked(self):
        self.page.evaluate('document.querySelector("article").insertAdjacentHTML("beforeend",\'<button>Share</button><button>Share</button>\')')
        with self.assertRaises(NeedsAttention):self.share()
    def test_changed_metadata_never_used(self):
        self.later('document.querySelector("p").textContent="Wrong app"',100)
        with self.assertRaises(NeedsAttention):self.share()
        self.assertEqual(self.requests,['GET'])
    def test_replaced_card_even_identical_is_rejected(self):
        self.later('let a=document.querySelector("article");a.replaceWith(a.cloneNode(true))',100)
        with self.assertRaises(NeedsAttention):self.share()
        self.assertEqual(self.requests,['GET'])
    def test_create_share_is_never_clicked(self):
        self.page.evaluate('document.querySelector("article").insertAdjacentHTML("beforeend",\'<button onclick="window.bad=true">Create Share</button>\')')
        with self.assertRaises(NeedsAttention):self.share(.2)
        self.assertIsNone(self.page.evaluate('window.bad'));self.assertEqual(self.requests,['GET'])
class HeadlessShareTests(ShareFixtures,unittest.TestCase):pass
class HeadedShareTests(ShareFixtures,unittest.TestCase):headless=False
