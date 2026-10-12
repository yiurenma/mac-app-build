"""Budget boundary regressions with a deterministic clock; no network."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from appbuilder.jenkins import JenkinsBrowser,NeedsAttention,_observe_page,authenticated_console_response
JOB='https://ci.fixture.invalid/job/p/'
class ReadinessDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock=0
        self.b=JenkinsBrowser.__new__(JenkinsBrowser)
        self.b.page=SimpleNamespace(url=JOB,wait_for_timeout=Mock())
        self.b.context=SimpleNamespace(request=Mock())
        self.b.conf={'build_timeout_minutes':.1};self.b.result_poll=2;self.b.site='plugin'
        self.b.visit=Mock(return_value=200)
        for target,value in [('time.monotonic',lambda:self.clock),('time.sleep',self.advance),('page_error_kind',lambda p:None)]:
            p=patch('appbuilder.jenkins.'+target,value);p.start();self.addCleanup(p.stop)
    def advance(self,seconds):self.clock+=seconds
    def test_read_finishing_after_deadline_is_not_accepted(self):
        def read():self.clock=3;return 2
        self.assertIsNone(_observe_page(self.b,read,deadline=1,target=JOB,once=True))
    def test_expired_observation_does_not_read(self):
        read=Mock(return_value=2)
        self.assertIsNone(_observe_page(self.b,read,deadline=0,target=JOB));read.assert_not_called()
    def test_old_document_probe_expiry_prevents_next_navigation(self):
        def observe(*a,**kw):
            if kw.get('once'):self.clock=10
            return None
        with patch('appbuilder.jenkins._observe_page',side_effect=observe),patch.object(JenkinsBrowser,'_fast_console_result',return_value=None) as console:
            with self.assertRaises(TimeoutError):self.b.wait_result(JOB,2)
        self.assertEqual(self.b.visit.call_count,1);self.assertEqual(console.call_count,1)
    def test_observation_expiry_prevents_console_request(self):
        def observe(*a,**kw):self.clock=10;return None
        with patch('appbuilder.jenkins._observe_page',side_effect=observe),patch.object(JenkinsBrowser,'_fast_console_result') as console:
            with self.assertRaises(TimeoutError):self.b.wait_result(JOB,2)
        console.assert_not_called();self.assertEqual(self.b.visit.call_count,1)
    def test_console_result_arriving_late_is_not_accepted(self):
        def console(*a,**kw):self.clock=10;return 'SUCCESS','Finished: SUCCESS'
        with patch('appbuilder.jenkins._observe_page',return_value=None),patch.object(JenkinsBrowser,'_fast_console_result',side_effect=console):
            with self.assertRaises(TimeoutError):self.b.wait_result(JOB,2)
    def test_expired_console_auth_retry_starts_no_further_requests(self):
        def get(*a,**kw):
            self.clock=10
            return SimpleNamespace(status=200,url='https://login.fixture.invalid/sso',headers={})
        self.b.context=SimpleNamespace(request=SimpleNamespace(get=Mock(side_effect=get)))
        with self.assertRaises(PlaywrightTimeout):authenticated_console_response(self.b,JOB+'2/consoleText',10000,deadline=1)
        self.assertEqual(self.b.context.request.get.call_count,1);self.b.visit.assert_not_called()
    def test_slow_first_control_read_reports_readiness_error(self):
        def read(*args):self.clock=3;return object()
        with patch.object(JenkinsBrowser,'_find_control_once',side_effect=read):
            with self.assertRaisesRegex(NeedsAttention,'观察预算'):self.b._find_control('branch')
    def test_navigation_during_read_rejects_value(self):
        def read():self.b.page.url=JOB+'other/';return 2
        with self.assertRaises(NeedsAttention):_observe_page(self.b,read,target=JOB)
    def test_error_page_appearing_during_read_rejects_value(self):
        with patch('appbuilder.jenkins.page_error_kind',side_effect=[None,'denied']):
            with self.assertRaises(NeedsAttention):_observe_page(self.b,lambda:2,target=JOB)
    def test_post_read_guard_expiry_rejects_value(self):
        def check(page):self.clock+=.6;return None
        with patch('appbuilder.jenkins.page_error_kind',side_effect=check):
            self.assertIsNone(_observe_page(self.b,lambda:2,deadline=1,target=JOB))
