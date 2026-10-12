"""Real headed/headless readiness regressions; no external requests or dispatch."""
import json
import os
import time
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from appbuilder.jenkins import JenkinsBrowser, NeedsAttention, _observe_page, _dispatch_evidence

JOB='https://ci.fixture.invalid/job/release/'
class ReadinessFixtures:
    headless=True
    @classmethod
    def setUpClass(cls):
        if not cls.headless and not os.environ.get('DISPLAY'):
            raise unittest.SkipTest('Headed Chromium requires DISPLAY; strict suite rejects skips')
        cls.engine=sync_playwright().start()
        cls.browser=cls.engine.chromium.launch(executable_path='/usr/bin/chromium',headless=cls.headless,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.engine.stop()
    def setUp(self):
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.page=self.context.new_page();self.html='<main id="content"></main>';self.status=200;self.requests=[]
        def route(r):
            self.requests.append((r.request.method,r.request.url))
            if r.request.method!='GET':raise AssertionError('No dispatch permitted')
            r.fulfill(status=self.status,content_type='text/html',body=self.html)
        self.context.route('**/*',route)
        self.b=JenkinsBrowser.__new__(JenkinsBrowser)
        self.b.page=self.page;self.b.context=self.context;self.b.site='plugin';self.b.conf={};self.b.credentials=None;self.b.manual_login=False;self.b.visible=not self.headless
    def delayed(self,body):
        self.html='<main id="content"></main><script>setTimeout(()=>document.querySelector("main").innerHTML='+json.dumps(body)+',250)</script>'
    def test_callback_reads_delayed_source_without_reload(self):
        self.delayed('<div>#2<br>Manual Callback ← Source #1</div>')
        self.assertEqual(self.b.find_callback(JOB,1),2)
        self.assertEqual(self.requests,[('GET',JOB)])
    def test_callback_wrong_source_is_not_adopted(self):
        self.html='<div>#2<br>Manual Callback ← Source #10</div>'
        self.assertIsNone(self.b.find_callback(JOB,1,deadline=time.monotonic()+.3))
    def test_callback_multiple_candidates_stop(self):
        self.html='<div>#2<br>Manual Callback ← Source #1</div><div>#3<br>Manual Callback ← Source #1</div>'
        with self.assertRaisesRegex(NeedsAttention,'多个 Callback'):self.b.find_callback(JOB,1)
    def test_callback_http_denial_stops(self):
        self.status=403
        with self.assertRaises(NeedsAttention):self.b.find_callback(JOB,1)
    def test_error_shell_never_supplies_callback(self):
        self.html='<h1>Access Denied</h1><div>#2<br>Manual Callback ← Source #1</div>'
        with self.assertRaises(NeedsAttention):self.b.find_callback(JOB,1)
    def test_observer_rejects_wrong_job(self):
        self.page.goto(JOB+'other/')
        with self.assertRaises(NeedsAttention):_observe_page(self.b,lambda:2,target=JOB)
    def test_observer_respects_short_outer_budget(self):
        self.page.goto(JOB);start=time.monotonic()
        self.assertIsNone(_observe_page(self.b,lambda:None,deadline=start+.15,target=JOB))
        self.assertLess(time.monotonic()-start,.8)
        self.assertEqual(self.requests,[('GET',JOB)])
    def test_unknown_waits_same_page_for_queue(self):
        self.delayed('<div id="buildQueue"><div class="task"><a href="'+JOB+'">Job</a><a href="/queue/item/7/">Queue</a></div></div>')
        self.b.visit(JOB)
        self.assertEqual(_dispatch_evidence(self.b,JOB,[]),([],[7]))
        self.assertEqual(self.requests,[('GET',JOB)])
    def test_unknown_multiple_new_numbers_stop(self):
        self.html='<a href="'+JOB+'2/">#2</a><a href="'+JOB+'3/">#3</a>';self.b.visit(JOB)
        with self.assertRaisesRegex(NeedsAttention,'多个新 Build'):_dispatch_evidence(self.b,JOB,[1])
    def test_duplicate_field_is_never_chosen(self):
        self.html='<label>ENTITIES<input></label><label>ENTITIES<input></label>';self.b.visit(JOB)
        with self.assertRaisesRegex(NeedsAttention,'多个'):self.b._find_control('ENTITIES')
    def test_missing_field_has_bounded_failure(self):
        self.b.visit(JOB);start=time.monotonic()
        with self.assertRaisesRegex(NeedsAttention,'未找到'):self.b._find_control('ENTITIES')
        self.assertLess(time.monotonic()-start,3)
    def test_wrong_job_queue_is_not_evidence(self):
        self.html='<div id="buildQueue"><div class="task"><a href="'+JOB+'other/">Job</a><a href="/queue/item/7/">Queue</a></div></div>';self.b.visit(JOB)
        self.assertEqual(self.b.queued_builds(JOB),[])
    def test_new_build_expired_budget_is_not_type_error(self):
        self.b.poll=.01
        with self.assertRaisesRegex(NeedsAttention,'未发现 Build Number'):
            self.b.detect_new_build(JOB,[],minutes=.0001)
    def test_error_http_cannot_supply_new_build(self):
        self.status=403;self.html='<a href="'+JOB+'2/">#2</a>';self.b.poll=.01
        with self.assertRaises(NeedsAttention):self.b.detect_new_build(JOB,[],minutes=.1)
    def test_label_without_control_waits_for_its_control(self):
        self.html='<section><span>ENTITIES</span></section><script>setTimeout(()=>document.querySelector("section").insertAdjacentHTML("beforeend","<input>"),250)</script>'
        self.b.visit(JOB)
        self.assertEqual(self.b._find_control('ENTITIES').count(),1)
    def test_new_number_after_observation_window_is_read_before_reload(self):
        self.delayed('<a href="'+JOB+'2/">#2</a>')
        self.html=self.html.replace(',250)',',2200)')
        self.b.poll=.3
        self.assertEqual(self.b.detect_new_build(JOB,[1],minutes=5/60),2)
        self.assertEqual(self.requests,[('GET',JOB)])
    def test_late_callback_on_existing_document_is_read_before_reload(self):
        self.delayed('<div>#2<br>Manual Callback ← Source #1</div>')
        self.html=self.html.replace(',250)',',2200)')
        self.b.visit(JOB)
        self.assertIsNone(_observe_page(self.b,lambda:None,target=JOB))
        self.assertEqual(self.b.find_callback(JOB,1,deadline=time.monotonic()+1),2)
        self.assertEqual(self.requests,[('GET',JOB)])
    def test_history_result_after_window_survives_poll_without_reload(self):
        self.delayed('<div><a href="'+JOB+'2/">#2</a><svg class="icon-success"></svg></div>')
        self.html=self.html.replace(',250)',',2200)');self.b.result_poll=2
        # Plain nonterminal raw console, so only delayed exact history counts.
        original=self.html
        self.context.unroute('**/*')
        def route(r):
            self.requests.append((r.request.method,r.request.url))
            r.fulfill(content_type='text/plain' if r.request.url.endswith('consoleText') else 'text/html',body='Still running' if r.request.url.endswith('consoleText') else original)
        self.context.route('**/*',route)
        with patch.object(JenkinsBrowser,'_fast_console_result',return_value=None):
            self.assertEqual(self.b.wait_result(JOB,2,minutes=6/60),('SUCCESS',''))
        self.assertEqual(sum(u==JOB for _,u in self.requests),1)
    def test_late_heading_with_callback_watch_survives_poll(self):
        self.context.unroute('**/*');self.b.result_poll=2
        def route(r):
            self.requests.append((r.request.method,r.request.url))
            body='<main>Running</main>'
            if r.request.url==JOB+'2/':
                body="""<main id="main-panel"><h1>#2<span id="icon"></span></h1></main><script>setTimeout(()=>document.querySelector("#icon").innerHTML='<svg class="icon-success"></svg>',2200)</script>"""
            r.fulfill(content_type='text/plain' if r.request.url.endswith('consoleText') else 'text/html',body=body)
        self.context.route('**/*',route)
        with patch.object(JenkinsBrowser,'_fast_console_result',return_value=None):
            self.assertEqual(self.b.wait_result(JOB,2,minutes=10/60,watch_callback=True),('SUCCESS',''))
        self.assertEqual(sum(u==JOB+'2/' for _,u in self.requests),1)
class HeadlessCallbackTests(ReadinessFixtures,unittest.TestCase):pass
class HeadedCallbackTests(ReadinessFixtures,unittest.TestCase):headless=False
