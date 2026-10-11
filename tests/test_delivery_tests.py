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
        self.mail={'kind':'outlook','request_id':'a'*32,'recipients':'work@example.com','confirm_send':True}
    def adapter(self):
        adapter=Mock();adapter.resolve.return_value={'email':'sender@example.com','id':'1'}
        adapter.submit.return_value={'status':'SUBMITTED','message_id':'message-1'}
        return adapter
    def test_email_click_sends_only_fixed_content_and_returns_submission_not_delivery(self):
        adapter=self.adapter()
        with patch('appbuilder.outlook.OutlookAdapter',return_value=adapter):dt.start(self.mail)
        result=dt.poll('a'*32)
        self.assertEqual(result['status'],'SUBMITTED')
        self.assertIn('尚未验证',result['result']['message'])
        payload=adapter.submit.call_args.args[0]
        self.assertIn('TEST',payload['subject']);self.assertNotIn('/join/',payload['body'])
        self.assertNotIn('password',json.dumps(payload))
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
    def test_test_email_requires_explicit_action_and_valid_target(self):
        for body in ({**self.mail,'confirm_send':False},{**self.mail,'recipients':'nickname'}):
            with self.assertRaises(ValueError):dt.start(body)
    def test_unapproved_testfairy_target_rejected_before_credentials_are_used(self):
        with patch('appbuilder.testfairy.TestFairyBrowser') as browser:
            with self.assertRaises(ValueError):dt.start({'kind':'testfairy','request_id':'c'*32,'url':'https://other.testfairy.com/login','username':'user','password':'SECRET'})
        browser.assert_not_called()
    def run_link_test(self,wanted):
        from test_testfairy import PARAMS,RUNTIME,expected_build
        browser=Mock();browser.page.url='https://hsbc-mobilex.testfairy.com/my?session=SECRET'
        browser.find.return_value={**expected_build(PARAMS,RUNTIME),'share_url':'https://hsbc-mobilex.testfairy.com/join/current','app_version':'3.76.0'}
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
