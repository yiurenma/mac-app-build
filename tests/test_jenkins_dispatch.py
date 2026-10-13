"""Offline browser dispatch receipts and resumable pre-submit state."""
import time
import unittest
from unittest.mock import patch,Mock
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from appbuilder.jenkins import JenkinsBrowser,NeedsAttention,_dispatch_or_reconcile

JOB='https://jenkins.invalid/job/release%252F123/'

class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=sync_playwright().start()
        cls.browser=cls.engine.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.engine.stop()
    def setUp(self):
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.worker=JenkinsBrowser.__new__(JenkinsBrowser)
        self.worker.page=self.context.new_page();self.worker.page.set_default_timeout(500)
        self.worker.site='plugin';self.worker.visible=False;self.worker.poll=.01
        self.worker.visit=lambda url:self.worker.page.goto(url)
        self.worker.detect_new_build=Mock(return_value=7)
        self.state={'runtime':{}};self.requests=[];self.status=201;self.entry=True;self.button=True;self.extra='';self.button_type='submit';self.prevent=False
        def route(r):
            self.requests.append((r.request.method,r.request.url))
            if r.request.method=='POST':
                r.fulfill(status=self.status,headers={'Location':'/queue/item/91/'} if self.status==201 else {},body='<main>Job</main>');return
            if r.request.url.endswith('/build?delay=0sec'):
                r.fulfill(content_type='text/html',body='<form method="post" action="build" '+('onsubmit="event.preventDefault()"' if self.prevent else '')+'>'+self.extra+'<button type="'+self.button_type+'">'+('Build' if self.button else 'Missing')+'</button></form>');return
            r.fulfill(content_type='text/html',body='<a href="build?delay=0sec">Build with Parameters</a>' if self.entry else '<main>No entry</main>')
        self.context.route('**/*',route)
        def save(task,**fields):self.state['runtime'].update(fields)
        for target,value in [('read_state',lambda task:self.state),('patch_runtime',save)]:
            p=patch('appbuilder.jenkins.'+target,value);p.start();self.addCleanup(p.stop)
    def run_build(self):return _dispatch_or_reconcile(self.worker,JOB,'t','plugin_formal',with_parameters=True)
    def test_first_parameter_build_dispatches_in_both_ui_modes(self):
        for visible in (False,True):
            with self.subTest(visible=visible):
                self.state['runtime']={};self.requests=[];self.worker.visible=visible
                self.assertEqual(self.run_build(),7)
                self.assertEqual(sum(m=='POST' for m,u in self.requests),1)
                self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'ACCEPTED')
                self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['queue_id'],91)
    def test_pre_submit_failure_resumes_and_submits_once(self):
        self.entry=False
        with self.assertRaises(NeedsAttention):self.run_build()
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'PREPARING')
        self.worker.detect_new_build.assert_not_called()
        self.entry=True
        self.assertEqual(self.run_build(),7)
        self.assertEqual(sum(m=='POST' for m,u in self.requests),1)
    def test_legacy_baseline_is_unknown_not_permission_to_resubmit(self):
        self.state['runtime']={'plugin_formal_baseline':[]}
        with self.assertRaisesRegex(NeedsAttention,'未知'):self.run_build()
        self.assertFalse(any(m=='POST' for m,u in self.requests));self.worker.detect_new_build.assert_not_called()
    def test_unknown_post_stops_without_unbounded_wait_or_resubmit(self):
        self.status=200
        with self.assertRaisesRegex(NeedsAttention,'结果未知'):self.run_build()
        with self.assertRaisesRegex(NeedsAttention,'结果未知'):self.run_build()
        self.assertEqual(sum(m=='POST' for m,u in self.requests),1)
        self.worker.detect_new_build.assert_not_called()
    def test_click_interruption_preserves_dispatching(self):
        self.worker.build_dispatch_checkpoint=None
        original=self.worker._safe_click
        def click(text,role,before_click=None):
            if before_click:
                control=self.worker.page.get_by_role('button',name='Build',exact=True)
                before_click(control);raise RuntimeError('interrupted after dispatch boundary')
            return original(text,role)
        self.worker._safe_click=click
        with self.assertRaises(RuntimeError):self.run_build()
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'DISPATCHING')
        with self.assertRaisesRegex(NeedsAttention,'未知'):self.run_build()
        self.worker.detect_new_build.assert_not_called()
    def test_existing_queue_is_reconciled_without_click(self):
        self.state['runtime']={'plugin_formal_baseline':[]}
        self.worker.queued_builds=Mock(return_value=[91])
        self.assertEqual(self.run_build(),7)
        self.assertFalse(any(m=='POST' for m,u in self.requests))
    def test_build_appearing_between_samples_blocks_dispatch_and_resume(self):
        self.worker.build_numbers=Mock(side_effect=[set(),{4}])
        with self.assertRaisesRegex(NeedsAttention,'历史已变化'):self.run_build()
        with self.assertRaisesRegex(NeedsAttention,'历史变化'):self.run_build()
        self.assertFalse(any(m=='POST' for m,u in self.requests))
    def test_different_job_saved_receipt_never_reused(self):
        self.state['runtime']={'plugin_formal_baseline':[],'plugin_formal_dispatch':{'state':'PREPARING','job_url':JOB.replace('123','456')}}
        with self.assertRaisesRegex(NeedsAttention,'不一致'):self.run_build()
        self.assertFalse(self.requests)
    def test_queue_requires_same_row_exact_encoded_job(self):
        self.worker.page.goto(JOB)
        self.worker.page.set_content('<div id="buildQueue"><table><tr><td><a href="'+JOB+'">job</a><a href="https://jenkins.invalid/queue/item/91/">queued</a></td></tr><tr><td><a href="'+JOB.replace('%252F','%2F')+'">other</a><a href="https://jenkins.invalid/queue/item/92/">queued</a></td></tr></table></div>')
        self.assertEqual(self.worker.queued_builds(JOB),[91])

    def test_required_empty_field_is_pre_submit_failure_not_wait(self):
        self.extra='<input required name="required-value">'
        with self.assertRaisesRegex(NeedsAttention,'校验未通过'):self.run_build()
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'PREPARING')
        self.assertFalse(any(m=='POST' for m,u in self.requests));self.worker.detect_new_build.assert_not_called()
    def test_non_submit_button_is_rejected_before_dispatch(self):
        self.button_type='button'
        with self.assertRaisesRegex(NeedsAttention,'不是提交按钮'):self.run_build()
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'PREPARING')
        self.worker.detect_new_build.assert_not_called()
    def test_prevent_default_click_without_post_is_unknown_and_does_not_wait(self):
        self.prevent=True
        with self.assertRaisesRegex(NeedsAttention,'结果未知'):self.run_build()
        self.assertFalse(any(m=='POST' for m,u in self.requests))
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'UNKNOWN')
        self.worker.detect_new_build.assert_not_called()

    def test_preparing_resume_does_not_adopt_external_build(self):
        self.state['runtime']={'plugin_formal_baseline':[7], 'plugin_formal_dispatch':{'state':'PREPARING','job_url':JOB}}
        self.worker.build_numbers=Mock(return_value={7,8})
        with self.assertRaisesRegex(NeedsAttention,'不能认领'):self.run_build()
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'CONFLICT')
        self.worker.detect_new_build.assert_not_called()
        self.assertFalse(any(m=='POST' for m,u in self.requests))

    def test_gmx_repaint_changes_parameter_after_fill_blocks_post(self):
        self.worker.site='gmx';self.worker.conf={'gmx_fields':{name:'#'+name for name in ('ENTITIES','SYNC_UPLOAD','CUSTOM_FOLDER')}}
        self.extra='<input id="ENTITIES"><input type="checkbox" id="SYNC_UPLOAD"><input id="CUSTOM_FOLDER">'
        original=self.worker._safe_click
        def click(text,role,before_click=None):
            if before_click:
                check=before_click
                def changed(control):
                    self.worker.page.locator('#CUSTOM_FOLDER').fill('OTHER')
                    check(control)
                before_click=changed
            return original(text,role,before_click=before_click)
        self.worker._safe_click=click
        with self.assertRaisesRegex(NeedsAttention,'填写后被页面改变'):
            _dispatch_or_reconcile(self.worker,JOB,'t','gmx_formal',with_parameters=True,country='UK',project='PayByBank')
        self.assertFalse(any(m=='POST' for m,u in self.requests))
        self.assertEqual(self.state['runtime']['gmx_formal_dispatch']['state'],'PREPARING')

    def test_post_load_busy_initialization_waits_then_submits_once(self):
        self.prevent=True
        self.extra='<script>addEventListener("load",()=>{const f=document.querySelector("form");f.setAttribute("aria-busy","true");setTimeout(()=>{f.addEventListener("submit",()=>fetch("build",{method:"POST"}));f.removeAttribute("aria-busy");},350);});</script>'
        self.assertEqual(self.run_build(),7)
        self.assertEqual(sum(m=='POST' for m,u in self.requests),1)
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'ACCEPTED')

    def test_visible_disabled_submit_waits_until_enabled_and_posts_once(self):
        # Allow actionability polling/backoff after the fixture's 350ms delay.
        # The suite's 500ms negative-case timeout can expire before its next poll.
        self.worker.page.set_default_timeout(2000)
        self.extra='<script>addEventListener("DOMContentLoaded",()=>{document.querySelector("button").disabled=true;});</script>'
        original=self.worker._safe_click
        entered=[]
        def click(text,role,before_click=None):
            if role=='button':
                control=self.worker.page.get_by_role('button',name='Build',exact=True)
                self.assertTrue(control.is_disabled())
                self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'PREPARING')
                self.assertFalse(any(m=='POST' for m,u in self.requests))
                entered.append(True)
                # Start the timer at the wait boundary, not during page loading.
                self.worker.page.evaluate("setTimeout(()=>document.querySelector('button').disabled=false,350)")
                checked=before_click
                def enabled(control):
                    self.assertTrue(control.is_enabled())
                    self.assertFalse(any(m=='POST' for m,u in self.requests))
                    checked(control)
                before_click=enabled
            return original(text,role,before_click=before_click)
        self.worker._safe_click=click
        self.assertEqual(self.run_build(),7)
        self.assertEqual(entered,[True])
        self.assertEqual(sum(m=='POST' for m,u in self.requests),1)
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'ACCEPTED')

    def test_permanently_disabled_submit_times_out_before_dispatch(self):
        self.extra='<script>addEventListener("DOMContentLoaded",()=>{document.querySelector("button").disabled=true;});</script>'
        started=time.monotonic()
        with self.assertRaises(PlaywrightTimeoutError):self.run_build()
        self.assertLess(time.monotonic()-started,3)
        self.assertTrue(self.worker.page.get_by_role('button',name='Build',exact=True).is_disabled())
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'PREPARING')
        self.assertFalse(any(m=='POST' for m,u in self.requests))
        self.worker.detect_new_build.assert_not_called()

    def test_rejected_post_is_not_accepted_and_not_retried(self):
        self.status=403
        with self.assertRaisesRegex(NeedsAttention,'结果未知'):self.run_build()
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['http_status'],403)
        self.assertEqual(self.state['runtime']['plugin_formal_dispatch']['state'],'UNKNOWN')
        with self.assertRaisesRegex(NeedsAttention,'结果未知'):self.run_build()
        self.assertEqual(sum(m=='POST' for m,u in self.requests),1)
        self.worker.detect_new_build.assert_not_called()

    def test_gmx_disabled_final_field_is_not_submitted(self):
        self.worker.site='gmx';self.worker.conf={'gmx_fields':{name:'#'+name for name in ('ENTITIES','SYNC_UPLOAD','CUSTOM_FOLDER')}}
        self.extra='<input id="ENTITIES"><input type="checkbox" id="SYNC_UPLOAD"><input id="CUSTOM_FOLDER">'
        original=self.worker._safe_click
        def click(text,role,before_click=None):
            if before_click:
                check=before_click
                def disabled(control):
                    self.worker.page.locator('#CUSTOM_FOLDER').evaluate('(e)=>e.disabled=true')
                    check(control)
                before_click=disabled
            return original(text,role,before_click=before_click)
        self.worker._safe_click=click
        with self.assertRaisesRegex(NeedsAttention,'已禁用'):
            _dispatch_or_reconcile(self.worker,JOB,'t','gmx_formal',with_parameters=True,country='UK',project='PayByBank')
        self.assertFalse(any(m=='POST' for m,u in self.requests))
