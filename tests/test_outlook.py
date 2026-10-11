import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from appbuilder.outlook import recipients,preferences,OutlookAdapter,notify_outcome,outcome_preview
from appbuilder.jenkins import NeedsAttention

class OutlookTests(unittest.TestCase):
    def test_recipients_are_explicit_addresses(self):
        self.assertEqual(recipients('a@example.com; b@example.com,a@example.com'),['a@example.com','b@example.com'])
        for value in ('Alice','a@example.com\nBcc: evil@example.com','Alice <a@example.com>','a..b@example.com','a@example..com'):
            with self.assertRaises(ValueError):recipients(value)
    def test_sender_unique_or_explicit_selection(self):
        adapter=OutlookAdapter();adapter.probe=Mock(return_value={'accounts':[{'email':'work@example.com','id':'1'}]})
        self.assertEqual(adapter.resolve()['email'],'work@example.com')
        adapter.probe.return_value['accounts'].append({'email':'personal@example.com','id':'2'})
        with self.assertRaises(NeedsAttention):adapter.resolve()
        self.assertEqual(adapter.resolve('work@example.com')['id'],'1')
        with self.assertRaises(NeedsAttention):adapter.resolve('missing@example.com')
    def test_non_mac_probe_does_not_attempt_send(self):
        with patch('appbuilder.outlook.sys.platform','linux'),patch('appbuilder.outlook.subprocess.run') as command:
            with self.assertRaises(NeedsAttention):OutlookAdapter().probe()
        command.assert_not_called()
    def test_auto_send_requires_recipients_but_can_notify_build_failure_without_testfairy(self):
        with self.assertRaises(ValueError):preferences({'outlook_auto_send':True})
        self.assertTrue(preferences({'outlook_auto_send':True,'outlook_recipients':'a@example.com'})['enabled'])
    def test_failure_preview_has_no_wrong_app_link_or_secret(self):
        row={'id':'task','status':'FAILED','stage':'GMX_JENKINS','note':'password=TOPSECRET','created':'today'}
        state={'completed':['PLUGIN_JENKINS'],'runtime':{'testfairy_app':{'share_url':'https://tenant.testfairy.com/join/wrong'}}}
        preview=outcome_preview(row,{'outlook':{'to':['a@example.com']}},state,'token=ABC\nerror')
        self.assertNotIn('TOPSECRET',preview['body']);self.assertNotIn('token=ABC',preview['body'])
        self.assertNotIn('/join/wrong',preview['body']);self.assertIn('GMX_JENKINS',preview['body'])
    def _notify_fixture(self):
        state={'runtime':{},'completed':['PLUGIN_JENKINS']}
        params={'outlook':{'enabled':True,'to':['r@example.com'],'account':''},'country':'UK','project':'P'}
        row={'id':'task','status':'FAILED','stage':'GMX_JENKINS','note':'reason','created':'today','params':json.dumps(params)}
        adapter=Mock();adapter.resolve.return_value={'email':'work@example.com','id':'1'}
        adapter.submit.return_value={'status':'SUBMITTED','message_id':'9'}
        return state,row,adapter
    def test_same_outcome_once_and_failure_to_success_notifies_again(self):
        state,row,adapter=self._notify_fixture()
        with tempfile.TemporaryDirectory() as td,patch('appbuilder.store.task',return_value=row),patch('appbuilder.store.read_state',side_effect=lambda _:copy.deepcopy(state)),patch('appbuilder.store.patch_runtime',side_effect=lambda _,**v:state['runtime'].update(v)),patch('appbuilder.store.task_folder',return_value=Path(td)):
            notify_outcome('task',adapter);notify_outcome('task',adapter)
            self.assertEqual(adapter.submit.call_count,1)
            row.update(status='SUCCESS',stage='FINISHED',note='success')
            notify_outcome('task',adapter)
            self.assertEqual(adapter.submit.call_count,2)
            self.assertEqual(len(state['runtime']['mail_notifications']),2)
    def test_unknown_send_does_not_retry_and_does_not_change_build_result(self):
        state,row,adapter=self._notify_fixture();adapter.submit.side_effect=TimeoutError('uncertain')
        with tempfile.TemporaryDirectory() as td,patch('appbuilder.store.task',return_value=row),patch('appbuilder.store.read_state',side_effect=lambda _:copy.deepcopy(state)),patch('appbuilder.store.patch_runtime',side_effect=lambda _,**v:state['runtime'].update(v)),patch('appbuilder.store.task_folder',return_value=Path(td)):
            notify_outcome('task',adapter);notify_outcome('task',adapter)
        self.assertEqual(adapter.submit.call_count,1)
        self.assertEqual(state['runtime']['mail_delivery']['status'],'UNKNOWN')
        self.assertEqual(row['status'],'FAILED')
    def test_unsupported_interface_preserves_preview_and_original_failure(self):
        state,row,adapter=self._notify_fixture();adapter.resolve.side_effect=NeedsAttention('unsupported')
        with tempfile.TemporaryDirectory() as td,patch('appbuilder.store.task',return_value=row),patch('appbuilder.store.read_state',return_value=state),patch('appbuilder.store.patch_runtime',side_effect=lambda _,**v:state['runtime'].update(v)),patch('appbuilder.store.task_folder',return_value=Path(td)):
            notify_outcome('task',adapter)
        adapter.submit.assert_not_called();self.assertEqual(row['status'],'FAILED')
        self.assertEqual(state['runtime']['mail_delivery']['status'],'BLOCKED')
        self.assertIn('mail_preview',state['runtime'])
    def test_testfairy_failure_notifies_without_link(self):
        state,row,adapter=self._notify_fixture();row.update(status='NEEDS_ATTENTION',stage='TESTFAIRY_LOOKUP',note='精确匹配超时')
        with tempfile.TemporaryDirectory() as td,patch('appbuilder.store.task',return_value=row),patch('appbuilder.store.read_state',return_value=state),patch('appbuilder.store.patch_runtime',side_effect=lambda _,**v:state['runtime'].update(v)),patch('appbuilder.store.task_folder',return_value=Path(td)):
            notify_outcome('task',adapter)
        payload=adapter.submit.call_args.args[0]
        self.assertIn('TESTFAIRY_LOOKUP',payload['body']);self.assertNotIn('/join/',payload['body'])
        self.assertEqual(row['status'],'NEEDS_ATTENTION')

    def test_worker_failure_runs_notification_hook_without_replacing_failure(self):
        from contextlib import nullcontext
        from appbuilder.runner import run
        error=NeedsAttention('original build failure')
        with patch('appbuilder.runner.worker_lease',return_value=nullcontext()),patch('appbuilder.runner._run_unlocked',side_effect=error),patch('appbuilder.runner.task',return_value={'status':'NEEDS_ATTENTION'}),patch('appbuilder.outlook.notify_outcome') as notify:
            with self.assertRaisesRegex(NeedsAttention,'original build failure'):
                run('task')
        notify.assert_called_once_with('task')
