"""Execute production JXA source under a Node mock host; no macOS app or mail is used."""
import json
import errno
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder.outlook import SEND_JS, OutlookAdapter, OutlookCallError


class NativeScriptTests(unittest.TestCase):
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

    def test_unknown_native_timeout_and_malformed_output_remain_uncertain(self):
        cases=[OSError(errno.EIO,'pipe error'),subprocess.TimeoutExpired('osascript',60),SimpleNamespace(returncode=1,stdout='',stderr='native failed password=SECRET'),
               SimpleNamespace(returncode=0,stdout='unexpected SECRET',stderr='')]
        for case in cases:
            with patch('appbuilder.outlook.subprocess.run',**({'side_effect':case} if isinstance(case,Exception) else {'return_value':case})):
                with self.assertRaises(OutlookCallError) as error:OutlookAdapter()._script(SEND_JS)
                self.assertFalse(error.exception.not_submitted)
                self.assertNotIn('SECRET',str(error.exception))

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
