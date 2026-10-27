"""Offline browser fixtures for loading/readiness; no company requests or login."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from appbuilder.jenkins import NeedsAttention
from appbuilder.testfairy import TestFairyBrowser
from chromium_path import CHROMIUM


BASE = 'https://hsbc-mobilex.testfairy.com'


class DirectoryLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = sync_playwright().start()
        cls.browser = cls.engine.chromium.launch(
            executable_path=CHROMIUM, headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.engine.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.worker = TestFairyBrowser(
            {'testfairy': {'wait_seconds': 6}}, Path(self.tmp.name),
            {'login_url': BASE + '/login', 'save_session': False},
            login_credentials=('offline@example.com', 'FAKE_PASSWORD'), visible=False)
        self.worker.context = self.context
        self.worker.page = self.context.new_page()
        self.requests = []
        self.pages = {}
        self.context.route('**/*', self.route)

    def route(self, route):
        self.requests.append(route.request.url)
        path = urlsplit(route.request.url).path
        if path == '/directory-api':
            route.fulfill(status=503, content_type='application/json',
                          body='{"password":"BODY_SECRET"}')
        else:
            route.fulfill(content_type='text/html', body=self.pages.get(path, '<p>Fixture</p>'))

    def folder_error(self, html, seconds=0.6):
        self.pages['/my'] = html
        started = time.monotonic()
        with self.assertRaises(NeedsAttention) as caught:
            self.worker._folder('IOS_Upload', deadline=started + seconds)
        self.assertLess(time.monotonic() - started, 2.5, 'directory wait exceeded its shared deadline')
        return str(caught.exception)

    def test_directory_loading_over_three_seconds_then_spinner_disappears(self):
        self.pages['/my'] = '''<div id="busy" role="progressbar">Loading</div>
        <div id="listing" aria-busy="true"></div><script>
        setTimeout(()=>{document.querySelector('#busy').remove();
        const list=document.querySelector('#listing');list.setAttribute('aria-busy','false');
        list.setAttribute('role','list');list.innerHTML='<a href="/target">IOS_Upload</a>';},3300);
        </script>'''
        expected = {'path': 'IOS_Upload', 'folder': 'fixture', 'number': 7,
                    'sha': 'a' * 40, 'build_url': 'https://build.example/job/upload/7/'}
        note = ('3.76.0 (130363)\nBuild number: 7\nBuild URL: ' + expected['build_url']
                + '\nFolder Param: fixture\nCommitSHA: ' + expected['sha'])
        self.pages['/target'] = '<article><pre>' + note + '</pre><a href="/join/current">Share</a></article>'
        started = time.monotonic()
        with patch.object(self.worker, 'login'):
            result = self.worker.find(expected)
        self.assertGreater(time.monotonic() - started, 3)
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual(result['share_url'], BASE + '/join/current')
        self.assertEqual(sum(urlsplit(url).path == '/my' for url in self.requests), 1)

    def test_permanent_spinner_is_bounded_loading_timeout_not_missing_folder(self):
        message = self.folder_error('<div role="progressbar">Loading</div><div role="tree" aria-busy="true"></div>')
        self.assertIn('超时', message)
        self.assertRegex(message, '加载|loading')
        self.assertNotIn('目录不存在', message)
        self.assertNotIn('已就绪', message)

    def test_same_origin_xhr_http_failure_has_status_without_sensitive_query_or_body(self):
        message = self.folder_error('''<div role="progressbar">Loading</div><script>
        fetch('/directory-api?token=QUERY_SECRET').then(r=>r.text());</script>''')
        self.assertIn('503', message)
        self.assertNotIn('QUERY_SECRET', message)
        self.assertNotIn('BODY_SECRET', message)
        self.assertNotIn('token=', message)

    def test_ready_empty_tree_is_distinct_from_unknown_readiness(self):
        ready = self.folder_error('<div role="tree" aria-busy="false"></div>')
        unknown = self.folder_error('<main><h1>APPS</h1><p>Unclassified fixture</p></main>')
        self.assertNotEqual(ready, unknown)
        self.assertRegex(ready, '就绪|ready|空')
        self.assertRegex(unknown, '未知|状态不明|无法确认|unknown')
        self.assertNotIn('目录不存在', unknown)

    def test_spinner_becomes_explicit_error_and_stops_with_safe_diagnostic(self):
        message = self.folder_error('''<main><div id="state" role="progressbar">Loading</div></main>
        <script>setTimeout(()=>{let e=document.querySelector('#state');e.setAttribute('role','alert');
        e.textContent='Unable to load directory password=FAKE_SECRET';},100)</script>''')
        self.assertIn('明确加载错误', message)
        self.assertIn('Unable to load directory', message)
        self.assertNotIn('FAKE_SECRET', message)

    def test_spinner_becomes_ready_empty_list(self):
        message = self.folder_error('''<main><div id="state" role="progressbar">Loading</div></main>
        <script>setTimeout(()=>{let e=document.querySelector('#state');e.setAttribute('role','tree');
        e.setAttribute('aria-busy','false');e.textContent='';},100)</script>''')
        self.assertIn('列表已就绪', message)
        self.assertNotIn('仍观察到加载中', message)

    def test_multiple_visible_directory_candidates_never_choose_arbitrarily(self):
        message = self.folder_error('''<main><div role="tree" aria-busy="false">
        <a href="/wrong-a">IOS_Upload</a><a href="/wrong-b">IOS_Upload</a></div></main>''')
        self.assertIn('多个可见交互候选', message)
        self.assertIn('可见交互候选 2', message)
        self.assertEqual([urlsplit(url).path for url in self.requests], ['/my'])

    def test_multiple_directories_and_share_use_one_total_budget(self):
        import json
        self.worker.config['testfairy']['wait_seconds'] = 3
        expected = {'path': 'IOS_Upload/IOS_Develop', 'folder': 'fixture', 'number': 7,
                    'sha': 'a' * 40, 'build_url': 'https://build.example/job/upload/7/'}
        def delayed_link(label, target):
            return ('<div id="list" aria-busy="true"></div><script>setTimeout(()=>{'
                    'let e=document.querySelector("#list");e.setAttribute("aria-busy","false");'
                    'e.innerHTML='+json.dumps('<a href="'+target+'">'+label+'</a>')+';},450)</script>')
        self.pages['/my'] = delayed_link('IOS_Upload', '/level2')
        self.pages['/level2'] = delayed_link('IOS_Develop', '/app')
        note = ('3.76.0 (130363)\nBuild number: 7\nBuild URL: ' + expected['build_url']
                + '\nFolder Param: fixture\nCommitSHA: ' + expected['sha'])
        dialog = '<div role="dialog"><a href="/join/current">Share</a></div>'
        onclick = 'window.shareClicked=true;setTimeout(()=>document.body.insertAdjacentHTML("beforeend",'+json.dumps(dialog)+'),2600)'
        self.pages['/app'] = '<article><pre>'+note+"</pre><button onclick='"+onclick+"'>Share</button></article>"
        started = time.monotonic()
        with patch.object(self.worker, 'login'), self.assertRaises(NeedsAttention) as caught:
            self.worker.find(expected)
        elapsed = time.monotonic()-started
        self.assertTrue(self.worker.page.evaluate('window.shareClicked === true'))
        self.assertRegex(str(caught.exception), 'Share|总等待预算已用尽')
        self.assertGreaterEqual(elapsed, 2.9)
        self.assertLess(elapsed, 3.8, 'Share reset the total wait budget')
        self.assertIn(BASE+'/app', self.requests)
        self.assertEqual(sum(urlsplit(url).path == '/my' for url in self.requests), 1)
    def test_browser_timeout_at_budget_boundary_preserves_cause_and_budget(self):
        self.worker.page.goto(BASE+'/my')
        self.worker._phase='share'
        started=time.monotonic()
        with self.assertRaises(NeedsAttention) as caught:
            self.worker._lookup_action(self.worker.page.locator('#absent').inner_text,started+.01)
        error=self.worker._diagnosed_error(caught.exception)
        self.assertIn('总等待预算已用尽',str(error))
        first=error.testfairy_diagnostics['first_error']
        self.assertEqual(first['type'],'TimeoutError')
        self.assertEqual(first['phase'],'share')
        self.assertIn('Locator.inner_text',first['message'])
        self.assertNotIn('总等待预算已用尽',first['message'])
        self.assertLess(time.monotonic()-started,1)

    def test_plain_card_label_reports_structure_without_click_or_sensitive_attributes(self):
        import json
        from appbuilder.testfairy import _FOLDER_STRUCTURE, _FOLDER_SELECTOR
        self.pages['/my'] = '''<aside>SIDEBAR_SECRET</aside><main><div class="CLASS_SECRET" data-secret="ATTR_SECRET">
        <span aria-label="ARIA_SECRET">IOS_Upload</span></div><div hidden>IOS_Upload</div></main>'''
        message = self.folder_error(self.pages['/my'], seconds=.3)
        structure = self.worker.page.evaluate(_FOLDER_STRUCTURE, {'label':'IOS_Upload','selector':_FOLDER_SELECTOR})
        self.assertEqual(structure['scope'], 'main')
        self.assertEqual(structure['exactVisibleLabels'], 1)
        self.assertEqual(structure['nodes'][0]['node']['tag'], 'span')
        self.assertIn('outside_selector',structure['nodes'][0]['exclusionReasons'])
        self.assertIn('accessible_name_mismatch',structure['nodes'][0]['exclusionReasons'])
        self.assertIn('可见交互候选 0',message)
        self.assertIn('目标名称只读结构',message)
        for secret in ('SIDEBAR_SECRET','CLASS_SECRET','ATTR_SECRET','ARIA_SECRET'):
            self.assertNotIn(secret,message+json.dumps(structure))
        self.assertEqual([urlsplit(url).path for url in self.requests], ['/my'])

    def test_network_pending_alone_is_not_dom_loading_evidence(self):
        from appbuilder.jenkins import NeedsAttention
        self.pages['/my']='<main><div role="tree" aria-busy="false"></div></main>'
        self.worker._observe_loading()
        self.worker.page.goto(BASE+'/my')
        self.worker._pending[123456]={'type':'fetch','started':time.monotonic()-30}
        self.worker._pending[123457]={'type':'xhr','started':time.monotonic()-20}
        state=self.worker._lookup_state()
        self.assertFalse(state['busy'])
        self.assertTrue(state['ready'])
        self.assertGreaterEqual(state['pending_requests'],2)
        self.assertEqual(state['pending_structure'][0]['type'],'fetch')
        self.assertGreaterEqual(state['pending_structure'][0]['age_seconds'],30)
        self.assertEqual(state['pending_structure'][1]['type'],'xhr')
        with self.assertRaises(NeedsAttention) as error:
            self.worker._lookup_failure('目录 IOS_Upload',state)
        self.assertIn('列表已就绪',str(error.exception))
        self.assertIn('不能据此判断目录仍在加载',str(error.exception))
        self.assertIn('不采集请求地址或内容',str(error.exception))
        self.assertIn('fetch',str(error.exception))
