import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
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
        self.mail={'task_id':self.task_id,'kind':'outlook','request_id':'a'*32,'recipients':'work@example.com','confirm_send':True}
    def adapter(self):
        adapter=Mock();adapter.resolve.return_value={'email':'sender@example.com','id':'1'}
        adapter.submit.return_value={'status':'SUBMITTED','message_id':'message-1'}
        return adapter
    def test_email_click_sends_history_report_without_rebuilding_or_changing_notifications(self):
        adapter=self.adapter()
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):dt.start(self.mail)
        result=dt.poll('a'*32)
        self.assertEqual(result['status'],'SUBMITTED')
        self.assertIn('尚未验证',result['result']['message'])
        payload=adapter.submit.call_args.args[0]
        self.assertIn('TEST',payload['subject']);self.assertNotIn('/join/',payload['body'])
        self.assertNotIn('SECRET',json.dumps(payload))
        self.assertIn(self.task_id,payload['body'])
        self.assertIn('GMX_JENKINS',payload['body'])
    def test_pre_send_error_is_blocked_with_diagnostics_not_unknown(self):
        from appbuilder.outlook import OutlookCallError
        adapter=self.adapter();adapter.submit.side_effect=OutlookCallError('set_recipients','password=SECRET',not_submitted=True,error_number=-1708)
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter),patch('appbuilder.store.patch_runtime') as write:
            dt.start(self.mail)
        result=dt.poll('a'*32)
        self.assertEqual(result['status'],'BLOCKED')
        self.assertFalse(result['result']['send_attempted'])
        self.assertEqual(result['result']['error_number'],-1708)
        self.assertNotIn('SECRET',json.dumps(result))
        write.assert_not_called()

    def test_test_report_requires_history_and_does_not_restart_build(self):
        with patch('appbuilder.web.create_build') as build:
            with self.assertRaisesRegex(ValueError,'历史构建'):dt.start({**self.mail,'task_id':''})
        build.assert_not_called()

    def test_account_probe_failure_preserves_pre_send_stage(self):
        from appbuilder.outlook import OutlookCallError
        adapter=self.adapter()
        adapter.resolve.side_effect=OutlookCallError('probe_accounts','账号集合为空',not_submitted=True)
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):dt.start(self.mail)
        result=dt.poll('a'*32)
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['result']['stage'],'probe_accounts')
        self.assertFalse(result['result']['send_attempted'])
        adapter.submit.assert_not_called()

    def test_duplicate_id_and_concurrent_clicks_do_not_send_twice(self):
        adapter=self.adapter()
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):
            dt.start(self.mail);dt.start(self.mail)
            with self.assertRaisesRegex(ValueError,'30 秒'):dt.start({**self.mail,'request_id':'b'*32})
        self.assertEqual(adapter.submit.call_count,1)
    def test_unknown_test_send_is_not_replayed_after_lookup(self):
        adapter=self.adapter();adapter.submit.side_effect=TimeoutError()
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):
            dt.start(self.mail);dt.start(self.mail)
        self.assertEqual(dt.poll('a'*32)['status'],'UNKNOWN');self.assertEqual(adapter.submit.call_count,1)
        with patch('appbuilder.delivery_tests.time.time',return_value=9999999999):
            with self.assertRaisesRegex(ValueError,'结果不明'):dt.start({**self.mail,'request_id':'e'*32})
    def test_unknown_guard_normalizes_recipient_order_duplicates_and_sender_case(self):
        adapter=self.adapter();adapter.submit.side_effect=TimeoutError('uncertain')
        original={**self.mail,'recipients':'One@example.com, two@example.com','account':'SENDER@example.com'}
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):
            dt.start(original)
            with patch('appbuilder.delivery_tests.time.time',return_value=9999999999):
                for changed in ({'recipients':'two@example.com, One@example.com'},
                                {'account':'sender@EXAMPLE.COM'},
                                {'recipients':'TWO@example.com, one@example.com, one@example.com','account':'sender@example.com'}):
                    with self.assertRaisesRegex(ValueError,'结果不明'):
                        dt.start({**original,**changed,'request_id':'e'*32})
        self.assertEqual(adapter.submit.call_count,1)

    def test_explicit_repeat_creates_audited_attempt_preserves_unknown_and_is_idempotent(self):
        adapter=self.adapter();adapter.submit.side_effect=[TimeoutError('uncertain'),{'status':'SUBMITTED','message_id':'new-message'}]
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter),patch('appbuilder.store.patch_runtime') as normal_ledger:
            dt.start(self.mail)
            old=dt.poll('a'*32)
            repeat={**self.mail,'request_id':'b'*32,'allow_repeat_test':True}
            with patch('appbuilder.delivery_tests.time.time',return_value=9999999999):
                dt.start(repeat);dt.start(repeat)
                dt.start({**repeat,'allow_repeat_test':False})
        self.assertEqual(adapter.submit.call_count,2)
        self.assertEqual(dt.poll('a'*32),old)
        result=dt.poll('b'*32)
        self.assertEqual(result['status'],'SUBMITTED')
        self.assertTrue(result['result']['allow_repeat_test'])
        self.assertEqual(result['result']['repeated_unknown_ids'],['a'*32])
        normal_ledger.assert_not_called()

    def test_repeat_permission_is_per_request_and_retained_when_second_attempt_unknown(self):
        adapter=self.adapter();adapter.submit.side_effect=TimeoutError('uncertain')
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):
            dt.start(self.mail)
            with patch('appbuilder.delivery_tests.time.time',return_value=9999999999):
                dt.start({**self.mail,'request_id':'b'*32,'allow_repeat_test':True})
            with patch('appbuilder.delivery_tests.time.time',return_value=10000000099):
                with self.assertRaisesRegex(ValueError,'结果不明'):
                    dt.start({**self.mail,'request_id':'c'*32})
        self.assertEqual(dt.poll('b'*32)['result']['repeated_unknown_ids'],['a'*32])
        self.assertEqual(adapter.submit.call_count,2)

    def test_allow_repeat_never_bypasses_inflight_target_even_when_another_test_is_newer(self):
        now=2000
        with dt.connect() as db:
            db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',('d'*32,'outlook','other-fingerprint',0,'SUBMITTING',json.dumps({'to':['WORK@example.com'],'account':'sender@example.com'})))
            db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',('e'*32,'outlook','unrelated',1000,'SUBMITTED','{}'))
        dt._ACTIVE.add('d'*32)
        self.addCleanup(dt._ACTIVE.clear)
        with patch('appbuilder.delivery_tests.time.time',return_value=now),patch('appbuilder.outlook.OutlookAdapter') as adapter:
            with self.assertRaisesRegex(ValueError,'正在执行'):
                dt.start({**self.mail,'allow_repeat_test':True})
        adapter.assert_not_called()

    def test_repeat_option_must_be_boolean_and_does_not_bypass_cooldown(self):
        for value in ('true',1,None):
            with self.assertRaisesRegex(ValueError,'开关值'):
                dt.start({**self.mail,'allow_repeat_test':value})
        adapter=self.adapter();adapter.submit.side_effect=TimeoutError('uncertain')
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):
            dt.start(self.mail)
            with self.assertRaisesRegex(ValueError,'30 秒'):
                dt.start({**self.mail,'request_id':'b'*32,'allow_repeat_test':True})
        self.assertEqual(adapter.submit.call_count,1)

    def test_test_email_requires_explicit_action_and_valid_target(self):
        for body in ({**self.mail,'confirm_send':False},{**self.mail,'recipients':'nickname'}):
            with self.assertRaises(ValueError):dt.start(body)
    def test_unapproved_testfairy_target_rejected_before_credentials_are_used(self):
        with patch('appbuilder.testfairy.TestFairyBrowser') as browser:
            with self.assertRaises(ValueError):dt.start({'kind':'testfairy','request_id':'c'*32,'url':'https://other.testfairy.com/login','username':'user','password':'SECRET'})
        browser.assert_not_called()
    def run_link_test(self,wanted,error=None,diagnostics=None):
        from test_testfairy import PARAMS,RUNTIME,expected_build
        browser=Mock();browser.page.url='https://hsbc-mobilex.testfairy.com/my?session=SECRET'
        browser.find.return_value={**expected_build(PARAMS,RUNTIME),'share_url':'https://hsbc-mobilex.testfairy.com/join/current','app_version':'3.76.0'}
        browser.find.return_value['diagnostics']=diagnostics or {}
        if error:browser.find.side_effect=error
        browser.__enter__=Mock(return_value=browser);browser.__exit__=Mock(return_value=False)
        body={'kind':'testfairy','request_id':'c'*32,'url':'https://hsbc-mobilex.testfairy.com/login','username':'user','password':'SECRET',
              'task_id':'202610111000-abcdef','expected_share_url':wanted}
        with patch('appbuilder.testfairy.TestFairyBrowser',return_value=browser) as factory,patch('appbuilder.web.create_build') as build,patch('appbuilder.store.task',return_value={'params':json.dumps(PARAMS)}),patch('appbuilder.store.read_state',return_value={'runtime':RUNTIME}):
            dt.start(body)
        self.assertFalse(factory.call_args.args[2]['save_session'])
        browser.find.assert_called_once_with(expected_build(PARAMS,RUNTIME))
        build.assert_not_called();self.assertNotIn('SECRET',(Path(self.tmp.name)/'delivery-tests.sqlite').read_bytes().decode('latin1'))
        return dt.poll('c'*32)
    def test_expected_link_is_compared_to_formal_extraction_not_echoed(self):
        result=self.run_link_test('https://HSBC-MOBILEX.TESTFAIRY.COM:443/join/current')
        self.assertEqual(result['status'],'MATCH')
        self.assertEqual(result['result']['final_url'],'https://hsbc-mobilex.testfairy.com/my')
    def test_browser_failure_keeps_first_error_and_safe_log_references(self):
        from appbuilder.jenkins import NeedsAttention
        error=NeedsAttention('离线测试失败 SECRET user')
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
    def test_interrupted_test_checkpoint_reports_unknown_without_restarting(self):
        with dt.connect() as db:
            db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',('d'*32,'outlook','fingerprint',0,'SUBMITTING','{}'))
        self.assertEqual(dt.poll('d'*32)['status'],'UNKNOWN')
