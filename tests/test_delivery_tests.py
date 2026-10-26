import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import delivery_tests as dt

class ImmediateThread:
    def __init__(self,target,**kwargs):self.target=target
    def start(self):self.target()

class DeliveryTestAPIs(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for target,value in [('appbuilder.delivery_tests.DATA',Path(self.tmp.name)),('appbuilder.delivery_tests.threading.Thread',ImmediateThread)]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        dt._ACTIVE.clear()
        self.task_id='202610111000-abcdef'
        self.row={'id':self.task_id,'params':json.dumps({'country':'UK','project':'PayByBank'}),'status':'FAILED','stage':'GMX_JENKINS','created':'now','note':'password=SECRET'}
        for target,value in [('appbuilder.store.task',lambda _:self.row),('appbuilder.store.read_state',lambda _:{'completed':[],'runtime':{}})]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)

    def test_email_kind_is_gone(self):
        # Email now goes through Jira (jira_mail.py); the old Outlook test kind is rejected.
        with self.assertRaisesRegex(ValueError,'测试类型'):
            dt.start({'kind':'outlook','request_id':'a'*32,'task_id':self.task_id})

    def test_duplicate_id_and_quick_second_click_do_not_run_twice(self):
        body={'kind':'testfairy','request_id':'c'*32,'task_id':self.task_id,'expected_share_url':''}
        from test_testfairy import PARAMS,RUNTIME
        with patch('appbuilder.app_links.lookup_task_app',side_effect=ValueError('stop')) as lookup,patch('appbuilder.store.task',return_value={'params':json.dumps(PARAMS)}),patch('appbuilder.store.read_state',return_value={'runtime':RUNTIME}):
            dt.start(body);dt.start(body)
            with self.assertRaisesRegex(ValueError,'30 秒'):dt.start({**body,'request_id':'b'*32})
        self.assertEqual(lookup.call_count,1)

    def test_invalid_expected_link_is_rejected_before_browser(self):
        from test_testfairy import PARAMS,RUNTIME
        with patch('appbuilder.app_links.lookup_task_app') as lookup,patch('appbuilder.store.task',return_value={'params':json.dumps(PARAMS)}),patch('appbuilder.store.read_state',return_value={'runtime':RUNTIME}):
            with self.assertRaises(ValueError):
                dt.start({'kind':'testfairy','request_id':'c'*32,'task_id':self.task_id,'expected_share_url':'https://evil.invalid/join/not-app'})
        lookup.assert_not_called()
    def run_link_test(self,wanted,error=None,diagnostics=None,transport=None):
        from test_testfairy import PARAMS,RUNTIME,expected_build
        expected=expected_build(PARAMS,RUNTIME)
        app={**expected,'share_url':'https://hsbc-mobilex.testfairy.com/join/current',
             'source_job_url':PARAMS['gmx_job'],'source_build_number':RUNTIME['gmx_build_number'],
             'source_build_url':expected['build_url'],'downstream_build_url':'https://jenkins.invalid/job/upload-to-testfairy/job/master/42/',
             'app_version':None,'app_build':None,'diagnostics':diagnostics or {}}
        if transport:app['transport']=transport
        body={'kind':'testfairy','request_id':'c'*32,'task_id':self.task_id,'expected_share_url':wanted}
        with patch('appbuilder.app_links.lookup_task_app',return_value=app,side_effect=error) as lookup,patch('appbuilder.web.create_build') as build,patch('appbuilder.store.task',return_value={'params':json.dumps(PARAMS)}),patch('appbuilder.store.read_state',return_value={'runtime':RUNTIME}):
            dt.start(body)
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[3:5],(PARAMS,RUNTIME))
        self.assertTrue(lookup.call_args.kwargs['visible'])
        self.assertTrue(lookup.call_args.kwargs['prefer_token'])
        build.assert_not_called();self.assertNotIn('SECRET',(Path(self.tmp.name)/'delivery-tests.sqlite').read_bytes().decode('latin1'))
        return dt.poll('c'*32)
    def test_expected_link_is_compared_to_formal_extraction_not_echoed(self):
        result=self.run_link_test('https://hsbc-mobilex.testfairy.com/join/current')
        self.assertEqual(result['status'],'MATCH')
        self.assertIn('/42/',result['result']['downstream_build_url'])
    def test_optional_expected_link_extracts_without_testfairy_authentication(self):
        result=self.run_link_test('')
        self.assertEqual(result['status'],'EXTRACTED')
        self.assertEqual(result['result']['actual_share_url'],'https://hsbc-mobilex.testfairy.com/join/current')
        self.assertIsNone(result['result']['app']['app_version'])
        self.assertIn('方式：浏览器', result['result']['message'])

    def test_link_test_prefers_the_saved_jenkins_token_and_says_which_way_was_used(self):
        result=self.run_link_test('',transport='jenkins-api-token')
        self.assertIn('方式：Jenkins API Token', result['result']['message'])
        self.assertEqual(result['result']['transport'],'jenkins-api-token')
    def test_browser_failure_keeps_first_error_and_safe_log_references(self):
        from appbuilder.jenkins import NeedsAttention
        error=NeedsAttention('离线测试失败 password=SECRET')
        error.testfairy_diagnostics={'attempt_id':'attempt-1','log_path':'/tmp/trace.jsonl',
            'evidence_path':'/tmp/evidence.json','first_error':{'phase':'login','type':'TimeoutError','message':'password=SECRET'},
            'logging_error':{'type':'OSError','message':'disk unavailable'},'raw_dom':'PRIVATE'}
        result=self.run_link_test('https://hsbc-mobilex.testfairy.com/join/current',error=error)
        self.assertEqual(result['status'],'BLOCKED')
        diagnostic=result['result']['diagnostics']
        self.assertEqual(diagnostic['first_error']['stage'],'login')
        self.assertEqual(diagnostic['first_error']['type'],'TimeoutError')
        self.assertEqual(diagnostic['log_path'],'/tmp/trace.jsonl')
        self.assertIn('OSError',diagnostic['logging_error'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('SECRET',json.dumps(result))

    def test_success_exposes_trace_but_does_not_claim_version_pinning(self):
        result=self.run_link_test('https://hsbc-mobilex.testfairy.com/join/current',diagnostics={'attempt_id':'attempt-2','log_path':'/tmp/trace.jsonl'})
        self.assertEqual(result['result']['diagnostics']['attempt_id'],'attempt-2')
        self.assertIn('尚未独立验证',result['result']['link_semantics'])

    def test_different_share_path_reports_mismatch(self):
        result=self.run_link_test('https://hsbc-mobilex.testfairy.com/join/other')
        self.assertEqual(result['status'],'MISMATCH')
        self.assertTrue(result['result']['actual_share_url'].endswith('/current'))
    def test_query_semantics_are_not_discarded_for_comparison(self):
        result=self.run_link_test('https://hsbc-mobilex.testfairy.com/join/current?variant=1')
        self.assertEqual(result['status'],'MISMATCH')
    def test_missing_build_context_does_not_pick_latest_or_start_browser(self):
        with patch('appbuilder.testfairy.TestFairyBrowser') as browser:
            with self.assertRaisesRegex(ValueError,'历史任务'):
                dt.start({'kind':'testfairy','request_id':'c'*32,'username':'user','password':'SECRET','expected_share_url':'https://hsbc-mobilex.testfairy.com/join/old'})
        browser.assert_not_called()
    def test_interrupted_test_checkpoint_is_reported_without_restarting(self):
        with dt.connect() as db:
            db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',('d'*32,'testfairy','fingerprint',0,'RUNNING','{}'))
        self.assertEqual(dt.poll('d'*32)['status'],'INTERRUPTED')
