"""No Jenkins, Git or new Build is invoked by URL diagnostics."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder.config import load_config
from appbuilder.diagnose import parse_target, run_diagnosis, expectation
from appbuilder import diagnose, web
from appbuilder.cli import args_parser

C=load_config()
P=C['jenkins']['plugin_parent']+'release_1_3_202610101332/'
G=C['jenkins']['gmx_parent']+'upload%2Fauto202610101332/'

class URLChecks(unittest.TestCase):
    def test_plugin_job_and_numbered_build(self):
        self.assertEqual(parse_target(P,C).site,'plugin')
        self.assertEqual(parse_target(P,C).number,None)
        self.assertEqual(parse_target(P+'2/',C).number,2)
        self.assertEqual(parse_target(P,C,number=2).number,2)
    def test_gmx_encoded_job(self):
        self.assertEqual(parse_target(G,C).site,'gmx')
    def test_reject_other_domains_or_branches(self):
        for url in ['https://evil.example/job/p/2/', 'http://evil.example/job/p/2/',
                    'https://evil.example@'+P[len('https://'):],
                    C['jenkins']['plugin_parent'], P+'2/consoleText',
                    P+'2/?token=abc',P+'2/#fragment',P+'../2/', P+'2/3/']:
            with self.subTest(url=url),self.assertRaises(ValueError): parse_target(url,C)
    def test_mismatch_number(self):
        with self.assertRaises(ValueError):parse_target(P+'2/',C,number=1)
    def test_verdict(self):
        self.assertEqual(expectation('SUCCESS','SUCCESS','plugin')[0],'PASS')
        self.assertEqual(expectation('FAILURE','SUCCESS','plugin')[0],'FAIL')
        self.assertEqual(expectation('UNKNOWN','SUCCESS','gmx')[0],'UNKNOWN')
    def test_cli_command_and_options(self):
        a=args_parser().parse_args(['diagnose-jenkins',P+'2/','--expected','SUCCESS','--visible'])
        self.assertEqual(a.command,'diagnose-jenkins');self.assertTrue(a.visible)

class FakeJenkins:
    calls=[]
    def __init__(self, *args, **kwargs):
        FakeJenkins.calls.append(('init',kwargs));self.site='plugin';self.page=SimpleNamespace(url=P)
        self.context=SimpleNamespace(request=SimpleNamespace(get=self.get_console))
        self._history='SUCCESS';self._heading='SUCCESS'
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def visit(self,url,timeout=None):
        FakeJenkins.calls.append(('visit',url));self.page.url=url;return 200
    def build_numbers(self,job):return {1,2}
    def _history_result(self,job,number):
        FakeJenkins.calls.append(('history',number));return self._history
    def _heading_result(self,number):
        FakeJenkins.calls.append(('heading',number));return self._heading
    def get_console(self,*args,**kwargs):
        FakeJenkins.calls.append(('console',args));raise RuntimeError('Should skip long console')

class DiagLogic(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        FakeJenkins.calls=[]
    def test_existing_build_2_success_without_console_or_build(self):
        with patch.object(diagnose,'DATA',Path(self.temp.name)):
            report=run_diagnosis(P+'2/',expected='SUCCESS',config=C,factory=FakeJenkins)
        self.assertEqual(report['verdict'],'PASS');self.assertEqual(report['build_number'],2)
        self.assertEqual(report['actual'],'SUCCESS')
        self.assertNotIn('console',[c[0] for c in FakeJenkins.calls])
        self.assertEqual(len([c for c in FakeJenkins.calls if c[0]=='history']),1)
        self.assertTrue((Path(self.temp.name)/'diagnostics/last-result.json').is_file())
        self.assertIn('[VERDICT] PASS','\n'.join(report['logs']))
    def test_exact_build_number_does_not_use_build_one(self):
        class OneSuccessTwoFailure(FakeJenkins):
            def _history_result(self,job,number):
                return 'SUCCESS' if number==1 else 'FAILURE'
            def _heading_result(self,number):
                return 'SUCCESS' if number==1 else 'FAILURE'
        with patch.object(diagnose,'DATA',Path(self.temp.name)):
            report=run_diagnosis(P+'2/',config=C,factory=OneSuccessTwoFailure)
        self.assertEqual(report['actual'],'FAILURE')
        self.assertEqual(report['verdict'],'FAIL')
    def test_history_versus_heading_disagreement_not_pass(self):
        class Divergent(FakeJenkins):
            def _heading_result(self,number):return 'FAILURE'
        with patch.object(diagnose,'DATA',Path(self.temp.name)):
            report=run_diagnosis(P+'2/',config=C,factory=Divergent)
        self.assertEqual(report['verdict'],'UNKNOWN')
        self.assertIn('[CONFLICT]','\n'.join(report['logs']))
    def test_no_history_no_heading_checks_console_once(self):
        class LogOnly(FakeJenkins):
            def _history_result(self,job,number):return None
            def _heading_result(self,number):return None
            def get_console(self,*args,**kwargs):
                FakeJenkins.calls.append(('console',kwargs))
                return SimpleNamespace(status=200,url=P+'2/consoleText',text=lambda:'Started\nFinished: SUCCESS\n')
        with patch.object(diagnose,'DATA',Path(self.temp.name)):
            report=run_diagnosis(P+'2/',config=C,factory=LogOnly)
        self.assertEqual(report['verdict'],'PASS')
        self.assertEqual(len([c for c in FakeJenkins.calls if c[0]=='console']),1)
        self.assertIn('timeout',FakeJenkins.calls[-1][1])
    def test_not_found_logged_not_raises_and_no_build(self):
        class Missing(FakeJenkins):
            def visit(self,*args,**kw):return 404
        with patch.object(diagnose,'DATA',Path(self.temp.name)):
            report=run_diagnosis(P+'2/',config=C,factory=Missing)
        self.assertEqual(report['verdict'],'UNKNOWN')
        self.assertIn('[DIAG-ERROR]','\n'.join(report['logs']))

class UiContracts(unittest.TestCase):
    def test_form_exists_and_shows_no_build(self):
        html=(web.STATIC/'index.html').read_text();js=(web.STATIC/'app.js').read_text()
        self.assertIn('id="diagnose-form"',html)
        self.assertIn('id="diagnose-output"',html)
        self.assertIn('Jenkins URL Test',html)
        self.assertIn("'/api/diagnose/start'",js)
        self.assertIn('id="diagnose-gmx-form"',html)

if __name__=='__main__':unittest.main()

class HttpDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.token='diagnostics-csrf-example'
        cls.s=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler(cls.token))
        cls.port=cls.s.server_port
        cls.thread=threading.Thread(target=cls.s.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.s.shutdown();cls.s.server_close();cls.thread.join(timeout=2)
    def post(self,body,headers=None):
        import http.client
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        h={'Content-Type':'application/json','X-CSRF-Token':self.token,
           'Origin':f'http://127.0.0.1:{self.port}','Host':f'127.0.0.1:{self.port}'}
        h.update(headers or {})
        try:
            conn.request('POST','/api/diagnose',body=json.dumps(body).encode(),headers=h)
            r=conn.getresponse();return r.status,json.loads(r.read())
        finally:conn.close()
    def test_api_diagnostic_no_task_created(self):
        with patch.object(diagnose,'run_diagnosis',return_value={'verdict':'PASS','logs':['[RESULT] SUCCESS']}) as runner,\
             patch.object(web,'create_build') as create:
            status,report=self.post({'url':P+'2/','build_number':2,'expected':'SUCCESS'})
        self.assertEqual(status,200);self.assertEqual(report['verdict'],'PASS')
        runner.assert_called_once();create.assert_not_called()
    def test_api_requires_csrf(self):
        with patch.object(diagnose,'run_diagnosis') as run:
            status,_=self.post({'url':P+'2/'},{'X-CSRF-Token':'bad'})
        self.assertEqual(status,403);run.assert_not_called()
    def test_api_non_integer_number_rejected(self):
        with patch.object(diagnose,'run_diagnosis') as run:
            status,_=self.post({'url':P,'build_number':'../../bad'})
        self.assertEqual(status,400);run.assert_not_called()
    def test_api_wrong_origin_rejected(self):
        with patch.object(diagnose,'run_diagnosis') as run:
            status,_=self.post({'url':P},{'Origin':'https://external.example'})
        self.assertEqual(status,403);run.assert_not_called()
