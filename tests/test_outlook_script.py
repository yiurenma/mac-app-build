"""Execute production JXA source under a Node mock host; no macOS app or mail is used."""
import json
import errno
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder.outlook import PROBE_JS, SEND_JS, OutlookAdapter, OutlookCallError


class NativeScriptTests(unittest.TestCase):
    def test_production_probe_retains_partial_field_failures_and_collection_error_stage(self):
        for failure in ('field','collection',''):
            host='''
const failure=FAILURE;
const account={emailAddress:()=>{if(failure==='field')throw Error('private detail');return 'work@example.com';},id:()=>1};
const app={version:()=> 'fixture-version',exchangeAccounts:()=>[account],
 imapAccounts:()=>{if(failure==='collection')throw Error('password=SECRET');return [];},popAccounts:()=>[]};
globalThis.Application=()=>app;
'''.replace('FAILURE',json.dumps(failure))
            native=subprocess.run(['node','-e',host+PROBE_JS+'\nconsole.log(run([]));'],capture_output=True,text=True,check=True)
            payload=json.loads(native.stdout)
            if failure=='collection':
                self.assertEqual(payload['stage'],'probe_imapAccounts')
                with patch('appbuilder.outlook.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=native.stdout,stderr='')):
                    with self.assertRaises(OutlookCallError) as error:OutlookAdapter()._script(PROBE_JS)
                self.assertTrue(error.exception.not_submitted)
                self.assertNotIn('SECRET',str(error.exception))
            else:
                self.assertEqual(payload['status'],'OK')
                self.assertEqual(payload['collections'][0]['count'],1)
                self.assertEqual(payload['accounts'][0]['email'],None if failure else 'work@example.com')
                self.assertNotIn('private detail',native.stdout)

    def execute(self, failure):
        host=r'''
const failure=FAILURE;let attempts=0;
const fail=stage=>{if(stage===failure)throw Error('fixture '+stage+' password=SECRET');};
const payload={account:'work@example.com',account_id:'1',subject:'TEST',body:'safe',to:['to@example.com']};
globalThis.ObjC={import:()=>{},unwrap:x=>x};
globalThis.$={NSString:{stringWithContentsOfFileEncodingError:()=>JSON.stringify(payload)},NSUTF8StringEncoding:0};
const account={emailAddress:()=>payload.account,id:()=>1};
const msg={toRecipients:{push:()=>fail('set_recipients')},id:()=>{fail('message_id');return 42;}};
Object.defineProperty(msg,'account',{set:()=>fail('set_account')});
const app={exchangeAccounts:()=>{fail('resolve_account');return [account]},imapAccounts:()=>[],popAccounts:()=>[],
OutgoingMessage:()=>{fail('create_message');return msg},outgoingMessages:{push:()=>{}},ToRecipient:x=>x,
send:()=>{attempts++;fail('send')}};
if(['unrelated_id','matching_id','unreadable_email','changed_id'].includes(failure)){
 const extra={emailAddress:()=>{if(failure==='unreadable_email')throw Error('unreadable');return failure==='unrelated_id'?'other@example.com':payload.account;},id:()=>{if(failure==='changed_id')return 99;throw Error('missing id');}};
 app.exchangeAccounts=()=>[account,extra];
}
globalThis.Application=()=>app;
'''.replace('FAILURE',json.dumps(failure))
        result=subprocess.run(['node','-e',host+SEND_JS+"\nconsole.log(JSON.stringify({result:JSON.parse(run(['unused'])),attempts}));"],capture_output=True,text=True,check=True)
        return json.loads(result.stdout)

    def test_actual_send_script_stage_and_send_attempt_boundaries(self):
        for stage in ('resolve_account','create_message','set_account','set_recipients','message_id','send',''):
            with self.subTest(stage=stage):
                data=self.execute(stage)
                self.assertEqual(data['attempts'],int(stage in ('send','')))
                result=data['result']
                if not stage:
                    self.assertEqual(result['status'],'SUBMITTED')
                    continue
                self.assertEqual(result['stage'],stage)
                self.assertEqual(result['send_attempted'],stage=='send')
                with patch('appbuilder.outlook.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(result),stderr='')):
                    with self.assertRaises(OutlookCallError) as error:
                        OutlookAdapter()._script(SEND_JS)
                self.assertEqual(error.exception.not_submitted,stage!='send')
                self.assertNotIn('SECRET',str(error.exception))

    def test_send_rechecks_target_and_ignores_only_known_unrelated_invalid_id(self):
        unrelated=self.execute('unrelated_id')
        self.assertEqual(unrelated['result']['status'],'SUBMITTED')
        self.assertEqual(unrelated['attempts'],1)
        for failure in ('matching_id','unreadable_email','changed_id'):
            result=self.execute(failure)
            self.assertEqual(result['attempts'],0)
            self.assertEqual(result['result']['stage'],'resolve_account')
            self.assertFalse(result['result']['send_attempted'])

    def test_unknown_native_timeout_and_malformed_output_remain_uncertain(self):
        cases=[OSError(errno.EIO,'pipe error'),subprocess.TimeoutExpired('osascript',60),SimpleNamespace(returncode=1,stdout='',stderr='native failed password=SECRET'),
               SimpleNamespace(returncode=0,stdout='unexpected SECRET',stderr='')]
        for case in cases:
            with patch('appbuilder.outlook.subprocess.run',**({'side_effect':case} if isinstance(case,Exception) else {'return_value':case})):
                with self.assertRaises(OutlookCallError) as error:OutlookAdapter()._script(SEND_JS)
                self.assertFalse(error.exception.not_submitted)
                self.assertNotIn('SECRET',str(error.exception))

    def test_readonly_probe_native_failures_never_imply_send_attempt(self):
        cases=[subprocess.TimeoutExpired('osascript',60),OSError(errno.EIO,'pipe error'),
               SimpleNamespace(returncode=1,stdout='',stderr='password=SECRET'),
               SimpleNamespace(returncode=0,stdout='not JSON SECRET',stderr='')]
        for case in cases:
            with patch('appbuilder.outlook.subprocess.run',**({'side_effect':case} if isinstance(case,Exception) else {'return_value':case})):
                with self.assertRaises(OutlookCallError) as caught:OutlookAdapter()._script(PROBE_JS)
            error=caught.exception
            self.assertTrue(error.not_submitted)
            self.assertFalse(error.send_attempted)
            self.assertTrue(error.stage.startswith('probe_'))
            self.assertNotIn('SECRET',str(error))
            self.assertNotIn('发送可能已开始',str(error))
            self.assertNotIn('发件箱',str(error))

    def test_submit_private_payload_and_cleanup_cannot_override_success(self):
        adapter=OutlookAdapter()
        captured=[]
        def script(source,args):
            path=Path(args[0]);captured.append(path)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(json.loads(path.read_text())['account_id'],'1')
            return {'status':'SUBMITTED','message_id':'42'}
        with patch.object(adapter,'_script',side_effect=script):
            result=adapter.submit({'subject':'TEST','body':'body','to':['to@example.com']},{'email':'work@example.com','id':'1'})
        self.assertEqual(result['status'],'SUBMITTED');self.assertFalse(captured[0].exists())
        with patch.object(adapter,'_script',return_value=result),patch('appbuilder.outlook.os.unlink',side_effect=OSError('cleanup failed')):
            self.assertEqual(adapter.submit({}, {'email':'work@example.com','id':'1'}),result)
