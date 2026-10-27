"""Offline semantic version traversal fixtures; authenticated production DOM is not assumed."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from appbuilder.testfairy import TestFairyBrowser, parse_candidate
from appbuilder.jenkins import NeedsAttention
from chromium_path import CHROMIUM

BASE='https://hsbc-mobilex.testfairy.com'
EXPECTED={'path':'IOS_Upload/IOS_Develop/0_A_UK_PayByBank','folder':'0_A_UK_PayByBank','number':2,
          'sha':'a'*40,'build_url':'https://jenkins.example/job/upload%2FABC/2/'}

def metadata(number=2, folder=None):
    return ('3.76.0 (130425) Build number: '+str(number)+' Build URL: https://jenkins.example/job/upload%2FABC/'+str(number)+
            '/ Folder Param: '+(folder or EXPECTED['path'])+' CommitSHA: '+'a'*40)

def card(number=2, share='/join/history', collapsed=False):
    notes='<p>'+metadata(number)+'</p>'
    if collapsed:
        notes='<button onclick="this.nextElementSibling.hidden=false;this.remove()">Show release notes</button><section hidden>'+notes+'</section>'
    return '<article class="version-card">'+notes+'<a href="'+share+'">Share</a></article>'

class VersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=sync_playwright().start()
        cls.browser=cls.engine.chromium.launch(executable_path=CHROMIUM,headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.engine.stop()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.worker=TestFairyBrowser({'testfairy':{'wait_seconds':4}},Path(self.tmp.name),{'login_url':BASE+'/login'},login_credentials=('fixture','SECRET'),visible=False)
        self.worker.context=self.context;self.worker.page=self.context.new_page();self.pages={};self.paths=[]
        def route(r):
            path=r.request.url.removeprefix(BASE);self.paths.append(path)
            r.fulfill(content_type='text/html',body=self.pages.get(path,'<main>Fixture</main>'))
        self.context.route('**/*',route)
        self.worker._observe_loading()
    def find(self):
        with patch.object(self.worker,'login'),patch.object(self.worker,'_folder',side_effect=lambda *args:self.worker.page.goto(BASE+'/my')):
            return self.worker.find(EXPECTED)
    def test_inline_fields_full_path_and_identifiers_are_separate(self):
        result=parse_candidate(metadata(),EXPECTED)
        self.assertEqual(result['number'],2);self.assertEqual(result['app_build'],'130425')
        self.assertEqual(result['build_url'],EXPECTED['build_url'])
        self.assertIsNone(parse_candidate(metadata().replace('%2F','/'),EXPECTED))
        self.assertIsNone(parse_candidate(metadata().replace('%2F','%252F'),EXPECTED))
        self.assertIsNone(parse_candidate(metadata(folder=EXPECTED['path']+'_CUTTING_EDGE'),EXPECTED))
        self.assertIsNone(parse_candidate(metadata()+' Build number: 2',EXPECTED))
    def test_history_entry_expands_same_card_and_finds_older_build_on_next_page(self):
        self.pages['/my']='<div class="app-card">HSBC UK <a href="/versions">See all versions</a></div>'
        self.pages['/versions']=card(9,collapsed=True)+'<a href="/versions2">Next</a>'
        self.pages['/versions2']=card(2,collapsed=True)
        result=self.find()
        self.assertEqual(result['share_url'],BASE+'/join/history')
        self.assertIn('/versions2',self.paths)
        self.assertEqual(result['link_semantics'],'existing_link_version_binding_unverified')
    def test_over_one_hundred_cards_are_not_silently_truncated(self):
        self.worker.config['testfairy']['wait_seconds']=15
        self.pages['/my']=''.join(card(100+i,share='/join/wrong') for i in range(101))+card(2)
        self.assertEqual(self.find()['share_url'],BASE+'/join/history')
    def test_unknown_pagination_reports_incomplete_not_not_found(self):
        self.worker.config['testfairy']['wait_seconds']=10  # 1s raced the budget on slow CI runners
        self.pages['/my']='<div class="app-card"><a href="/versions">See all versions</a></div>'
        self.pages['/versions']=card(9)+'<div class="unknown-pagination">Older</div>'
        # Running out of budget inside a page action is the same incomplete traversal;
        # which wording wins depends on where the deadline lands on a loaded runner.
        with self.assertRaisesRegex(NeedsAttention,'INCOMPLETE|等待预算已用尽'):self.find()
    def test_multiple_apps_are_not_guessed(self):
        app='<div class="app-card"><a href="/versions">See all versions</a></div>'
        self.pages['/my']=app+app
        with self.assertRaisesRegex(NeedsAttention,'多个 App'):self.find()
        self.assertNotIn('/versions',self.paths)
    def test_unknown_share_query_is_blocked_without_rewriting(self):
        self.pages['/my']=card(2,share='/join/history?unknownVersion=130425')
        with self.assertRaisesRegex(NeedsAttention,'未知查询参数'):self.find()
    def test_delayed_notes_with_unchanged_button_label_are_clicked_once(self):
        self.pages['/my']='<div class="app-card"><a href="/versions">See all versions</a></div>'
        self.pages['/versions']='<article class="version-card"><button id="notes">Show release notes</button><div id="content"></div><a href="/join/history">Share</a></article><a href="/wrong-page">Next</a><script>window.noteClicks=0;document.querySelector("#notes").addEventListener("click",()=>{window.noteClicks++;setTimeout(()=>document.querySelector("#content").textContent='+json.dumps(metadata())+',300);});</script>'
        result=self.find()
        self.assertEqual(result['share_url'],BASE+'/join/history')
        self.assertEqual(self.worker.page.evaluate('window.noteClicks'),1)
        self.assertNotIn('/wrong-page',self.paths)
        events=[json.loads(line) for line in Path(result['diagnostics']['log_path']).read_text().splitlines()]
        self.assertTrue(any(e['stage']=='release_notes' and e['event']=='end' for e in events))
        self.assertTrue(any(e['stage']=='field_comparison' and e.get('actual',{}).get('number')=='2' for e in events))

    def test_load_more_waits_through_empty_transition_then_checks_new_cards(self):
        self.pages['/my']='<div class="app-card"><a href="/versions">See all versions</a></div>'
        self.pages['/versions']='<div id="cards">'+card(8)+'</div><button id="more">Load more</button><script>window.moreClicks=0;document.querySelector("#more").addEventListener("click",()=>{window.moreClicks++;document.querySelector("#cards").innerHTML="";setTimeout(()=>document.querySelector("#cards").innerHTML='+json.dumps(card(2))+',250);});</script>'
        self.assertEqual(self.find()['share_url'],BASE+'/join/history')
        self.assertEqual(self.worker.page.evaluate('window.moreClicks'),1)

    def test_failure_preserves_first_error_and_attempt_files(self):
        self.pages['/my']=card(2,share='/join/history?unknown=1')
        with self.assertRaises(NeedsAttention) as caught:self.find()
        diagnostics=caught.exception.testfairy_diagnostics
        self.assertEqual(diagnostics['first_error']['phase'],'share')
        self.assertIn('未知查询参数',diagnostics['first_error']['message'])
        self.assertTrue(Path(diagnostics['log_path']).is_file())
        self.assertTrue(Path(diagnostics['evidence_path']).is_file())
    def test_explicit_end_status_is_distinct_from_unknown_pagination(self):
        self.pages['/my']='<div class="app-card"><a href="/versions">See all versions</a></div>'
        self.pages['/versions']=card(9)+'<div role="status">No more versions</div>'
        with self.assertRaisesRegex(NeedsAttention,'NOT_FOUND'):self.find()

class TraceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.worker=TestFairyBrowser({},Path(self.tmp.name),{'login_url':BASE+'/login','correlation_id':'request-fixture'},login_credentials=('fixture','PRIVATE_SECRET'),visible=False)
    def test_browser_start_failure_has_attempt_and_first_error(self):
        with patch('appbuilder.testfairy.sync_playwright',side_effect=RuntimeError('launch PRIVATE_SECRET')):
            with self.assertRaises(NeedsAttention) as error:self.worker.__enter__()
        first=error.exception.testfairy_diagnostics['first_error']
        self.assertEqual(first['type'],'RuntimeError')
        self.assertEqual(first['phase'],'browser_start')
        self.assertNotIn('PRIVATE_SECRET',str(error.exception))
        self.assertTrue(Path(error.exception.testfairy_diagnostics['log_path']).is_file())
    def test_browser_timeout_keeps_original_type_before_safe_wrapper(self):
        import time
        from playwright.sync_api import TimeoutError as BrowserTimeout
        def timeout(**kwargs):raise BrowserTimeout('original timeout')
        self.worker._phase='release_notes'
        with patch.object(self.worker,'_lookup_state',return_value={'busy':False,'ready':False}):
            with self.assertRaises(NeedsAttention):self.worker._lookup_action(timeout,time.monotonic()+1)
        self.assertEqual(self.worker.trace.summary['first_error']['type'],'TimeoutError')
        self.assertEqual(self.worker.trace.summary['first_error']['phase'],'release_notes')
    def test_cleanup_cannot_replace_existing_failure(self):
        from unittest.mock import Mock
        self.worker.context=Mock();self.worker.context.close.side_effect=RuntimeError('cleanup issue')
        original=ValueError('original failure')
        self.assertFalse(self.worker.__exit__(ValueError,original,None))
        self.assertEqual(self.worker.trace.summary['first_error']['type'],'ValueError')
        self.assertEqual(self.worker.trace.summary['first_error']['message'],'original failure')
