from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from appbuilder.dispatch_identity import reconcile
from appbuilder.jenkins import NeedsAttention

JOB='https://ci.invalid/job/example/'

class ReceiptIdentityTests(unittest.TestCase):
    def setUp(self):
        self.state={'runtime':{}}
        self.browser=SimpleNamespace(request_build=Mock(side_effect=self.submit))
        self.response=dict(state='ACCEPTED',http_status=201,build_url=JOB+'7/')
    def submit(self,*args,**kwargs):
        self.browser.build_dispatch_checkpoint(state='DISPATCHING')
        self.browser.build_dispatch_checkpoint(**self.response)
    def update(self,task,**fields):self.state['runtime'].update(deepcopy(fields))
    def run_dispatch(self):return reconcile(self.browser,JOB,'task','plugin_formal',lambda _:self.state,self.update)
    def test_direct_receipt_and_resume_do_not_resubmit(self):
        self.assertEqual(self.run_dispatch(),7)
        self.assertEqual(self.run_dispatch(),7)
        self.assertEqual(self.browser.request_build.call_count,1)
        self.assertEqual(self.state['runtime']['plugin_formal_identity']['kind'],'DIRECT_BUILD')
    def test_exact_queue_executable_mapping(self):
        self.response=dict(state='ACCEPTED',http_status=201,queue_id=91,queue_url='https://ci.invalid/queue/item/91/')
        self.browser.resolve_queue_build=Mock(return_value=dict(queue_id=91,job_url=JOB,relation='executable',build_url=JOB+'8/'))
        self.assertEqual(self.run_dispatch(),8)
    def test_saved_queue_identity_still_requires_matching_receipt_context(self):
        self.response=dict(state='ACCEPTED',http_status=201,queue_id=91,
                           queue_url='https://ci.invalid/queue/item/91/')
        self.browser.resolve_queue_build=Mock(return_value=dict(queue_id=91,job_url=JOB,
            relation='executable',build_url=JOB+'8/'))
        self.assertEqual(self.run_dispatch(),8)
        for value in (None,'https://ci.invalid/other/queue/item/91/'):
            self.state['runtime']['plugin_formal_dispatch']['queue_url']=value
            with self.assertRaises(NeedsAttention):self.run_dispatch()
        self.assertEqual(self.browser.request_build.call_count,1)

    def test_other_queue_same_sha_or_unique_new_number_never_proves_ownership(self):
        for observed in ({'queue_id':92,'job_url':JOB,'relation':'executable','build_url':JOB+'8/'},
                         {'queue_id':91,'job_url':JOB,'relation':'same_sha','build_url':JOB+'8/'},
                         {'queue_id':91,'job_url':'https://ci.invalid/job/other/','relation':'executable','build_url':JOB+'8/'}):
            self.setUp();self.response=dict(state='ACCEPTED',http_status=201,queue_id=91,queue_url='https://ci.invalid/queue/item/91/')
            self.browser.resolve_queue_build=Mock(return_value=observed)
            self.browser.detect_new_build=Mock(return_value=8)
            with self.assertRaises(NeedsAttention):self.run_dispatch()
            with self.assertRaises(NeedsAttention):self.run_dispatch()
            self.browser.detect_new_build.assert_not_called()
            self.assertEqual(self.browser.request_build.call_count,1)
    def test_unknown_conflict_or_interrupted_submit_never_retries(self):
        for state in ('UNKNOWN','CONFLICT','DISPATCHING'):
            self.setUp();self.response=dict(state=state,http_status=201,build_url=JOB+'7/')
            with self.assertRaises(NeedsAttention):self.run_dispatch()
            with self.assertRaises(NeedsAttention):self.run_dispatch()
            self.assertEqual(self.browser.request_build.call_count,1)
    def test_saved_number_without_receipt_is_not_legacy_fallback(self):
        self.state['runtime']['plugin_formal_number']=7
        with self.assertRaises(NeedsAttention):self.run_dispatch()
        self.browser.request_build.assert_not_called()
    def test_two_same_sha_attempts_keep_their_own_receipts(self):
        first=self.run_dispatch();identity=deepcopy(self.state['runtime']['plugin_formal_identity'])
        self.setUp();self.response['build_url']=JOB+'8/'
        second=self.run_dispatch()
        self.assertEqual((first,second),(7,8))
        self.assertNotEqual(identity['attempt_id'],self.state['runtime']['plugin_formal_identity']['attempt_id'])
    def test_response_saved_before_interruption_can_resume(self):
        def crash(*args,**kwargs):self.submit();raise RuntimeError('interrupted')
        self.browser.request_build.side_effect=crash
        with self.assertRaises(RuntimeError):self.run_dispatch()
        self.assertEqual(self.run_dispatch(),7)
        self.assertEqual(self.browser.request_build.call_count,1)
