"""Offline safety and execution-sequencing checks for GMX Jenkins build tests."""
import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder import web
from appbuilder.config import load_config
from appbuilder.live_tests import (validate_branch, validate_options, job_candidates,
                                   validate_job_url, gmx_build_test)

CFG=load_config()
BRANCH='upload/UKPBB-203745-V8'
VERSION='1.3.1-alpha.1-SNAPSHOT.2'

class BranchValidation(unittest.TestCase):
    def test_accepts_screenshot_branch(self):
        self.assertEqual(validate_branch(BRANCH),BRANCH)
    def test_reject_malicious_and_non_gmx(self):
        for s in ('upload/../test','upload/a//b','upload/-oops','release/1.3',
                  'upload/a\n--help','upload/','--upload/b','upload/a%2Fb','upload/\\x'):
            with self.subTest(s=s),self.assertRaises(ValueError):validate_branch(s)
    def test_both_jenkins_url_encodings(self):
        a,b=job_candidates(BRANCH,CFG)
        self.assertIn('upload%2FUKPBB-203745-V8',a)
        self.assertIn('upload%252FUKPBB-203745-V8',b)
        self.assertEqual(validate_job_url(a,BRANCH,CFG),a)
        self.assertEqual(validate_job_url(b,BRANCH,CFG),b)
        with self.assertRaises(ValueError):validate_job_url(b.replace('V8','V7'),BRANCH,CFG)
    def test_removed_bootstrap_test_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '已移除'):
            web.start_live_test({'kind':'bootstrap','branch':BRANCH})

    def test_parameters(self):
        b,c,p,settings=validate_options({'branch':BRANCH})
        self.assertEqual((b,c,p),(BRANCH,'UK','PayByBank'))
        self.assertTrue(settings['SYNC_UPLOAD']);self.assertFalse(settings['KEEP_ALIVE'])
        for key,value in [('sync_upload','true'),('force',1),('country','UK; echo'),('build_type','')]:
            with self.subTest(key=key),self.assertRaises(ValueError):validate_options({'branch':BRANCH,key:value})
    def test_real_build_cannot_start_without_explicit_checkbox(self):
        with self.assertRaisesRegex(ValueError,'确认'):
            web.start_live_test({'kind':'gmx-build','branch':BRANCH})
    def test_invalid_job_url_rejected_before_thread_spawn(self):
        with self.assertRaises(ValueError):
            web.start_live_test({'kind':'gmx-build','branch':BRANCH,'confirm_trigger':True,
                                 'job_url':'https://not-company.example/job/branch/'})

class FakeBrowser:
    modes=[]
    calls=[]
    def __init__(self, *args, **kwargs):
        self.page=SimpleNamespace(title=lambda:'job');self.gmx_settings={}
        FakeBrowser.calls.append(('start',kwargs))
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def visit(self,url,timeout=None):FakeBrowser.calls.append(('visit',url));return 200
    def mode(self,url):return FakeBrowser.modes.pop(0)
    def build_numbers(self,url):return set()
    def request_build(self,url,*,with_parameters,**kwargs):
        FakeBrowser.calls.append(('trigger','param' if with_parameters else 'plain',kwargs))
        return {1} if with_parameters else set()
    def detect_new_build(self,url,before):return 2 if before else 1
    def wait_parameter_mode(self,url):FakeBrowser.calls.append(('parameter-ready',))
    def wait_result(self,url,number):FakeBrowser.calls.append(('wait',number));return 'SUCCESS',''

class ExecutionFlow(unittest.TestCase):
    def setUp(self):FakeBrowser.calls=[];FakeBrowser.modes=['build'];self.logs=[]
    def test_first_plain_build_then_parameterized_build(self):
        _,country,project,settings=validate_options({'branch':BRANCH})
        r=gmx_build_test(BRANCH,country,project,settings,self.logs.append,config=CFG,factory=FakeBrowser)
        self.assertEqual(r['init_build_number'],1)
        self.assertEqual(r['build_number'],2)
        self.assertEqual(r['verdict'],'PASS')
        calls=FakeBrowser.calls
        self.assertLess(calls.index(('trigger','plain',{})),calls.index(('parameter-ready',)))
        self.assertLess(calls.index(('parameter-ready',)),calls.index(('trigger','param',{'country':'UK','project':'PayByBank'})))
        self.assertNotIn(('wait',2),calls)
        self.assertEqual(r['actual'],'TRIGGERED')
        self.assertTrue(r['build_url'].endswith('/2/'))
        self.assertFalse(r['result_verified'])
        self.assertFalse(r['upload_verified'])
    def test_existing_initialization_must_not_repeat_build(self):
        class AlreadyStarted(FakeBrowser):
            def build_numbers(self,url):return {1}
        _,country,project,settings=validate_options({'branch':BRANCH})
        with self.assertRaisesRegex(Exception,'已有 Build'):
            gmx_build_test(BRANCH,country,project,settings,self.logs.append,config=CFG,factory=AlreadyStarted)
        self.assertFalse(any(x[0]=='trigger' for x in FakeBrowser.calls))
    def test_already_parameterized_skips_first_build(self):
        FakeBrowser.modes=['parameters'];_,country,project,settings=validate_options({'branch':BRANCH})
        r=gmx_build_test(BRANCH,country,project,settings,self.logs.append,config=CFG,factory=FakeBrowser)
        self.assertIsNone(r['init_build_number'])
        self.assertEqual([x[1] for x in FakeBrowser.calls if x[0]=='trigger'],['param'])
    def test_unknown_mode_refuses_to_click(self):
        FakeBrowser.modes=['unknown'];_,country,project,settings=validate_options({'branch':BRANCH})
        with self.assertRaisesRegex(Exception,'Build'):
            gmx_build_test(BRANCH,country,project,settings,self.logs.append,config=CFG,factory=FakeBrowser)
        self.assertFalse(any(x[0]=='trigger' for x in FakeBrowser.calls))


class LiveWebIntegration(unittest.TestCase):
    def test_ui_and_api_contract(self):
        from appbuilder import web
        html=(web.STATIC/'index.html').read_text()
        js=(web.STATIC/'app.js').read_text()
        self.assertNotIn('id="bootstrap-live-form"',html)
        self.assertNotIn('body.artifact_version',js)
        self.assertNotIn("$('bootstrap-live-form')",js)
        self.assertIn('id="gmx-build-live-form"',html)
        self.assertIn('upload/UKPBB-203745-V8',html)
        self.assertIn("'/api/live-tests/start'",js)
        self.assertIn('confirm_trigger:true',js)
        self.assertIn('/api/live-tests/status/',js)
    def test_http_rejects_jenkins_trigger_without_confirmation(self):
        import json,http.client,threading
        from http.server import ThreadingHTTPServer
        from appbuilder import web
        server=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler('unit-csrf'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            port=server.server_port
            def request(token):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                payload=json.dumps({'kind':'gmx-build','branch':BRANCH}).encode()
                conn.request('POST','/api/live-tests/start',payload,
                    {'Host':f'127.0.0.1:{port}','Content-Type':'application/json','X-CSRF-Token':token})
                r=conn.getresponse();data=r.read();conn.close()
                return r.status,data
            self.assertEqual(request('missing')[0],403)
            status,data=request('unit-csrf')
            self.assertEqual(status,400)
            self.assertIn('确认',data.decode())
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()

