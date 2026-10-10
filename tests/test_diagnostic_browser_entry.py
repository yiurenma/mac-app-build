"""Two independent real-browser diagnostic routes: safe, visible, manual SSO."""
import http.client
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from appbuilder import diagnose, web
from appbuilder.config import load_config
from appbuilder.jenkins import JenkinsBrowser

C = load_config()
P = C['jenkins']['plugin_parent'] + 'release_1_3_202610101332/2/'
G = C['jenkins']['gmx_parent'] + 'upload%2Fauto202610101332/2/'


class ManualLogin(unittest.TestCase):
    def test_manual_login_waits_without_credentials_or_click(self):
        initial = 'https://sso.corporate.hsbc/login'
        target = P
        class Page:
            url = initial
            def locator(self, css):
                return SimpleNamespace(count=lambda: 1 if self.url == initial else 0)
        page = Page()
        worker = SimpleNamespace(page=page, site='plugin', conf={'auth_allowed_hosts':['*.hsbc']},
                                 _cache_login=lambda: setattr(worker,'cached',True))
        def progress(_): page.url = target
        with patch('appbuilder.jenkins.time.sleep', side_effect=progress):
            self.assertTrue(JenkinsBrowser._wait_manual_login(worker, target))
        self.assertTrue(worker.cached)

    def test_reject_unapproved_login_host(self):
        worker = SimpleNamespace(page=SimpleNamespace(url='https://unsafe-site.example/login'),
                                 site='plugin', conf={'auth_allowed_hosts':['*.hsbc']})
        with self.assertRaisesRegex(Exception,'未批准主机'):
            JenkinsBrowser._wait_manual_login(worker, P)

    def test_diagnosis_always_uses_visible_chrome_and_manual_login(self):
        class Browser:
            received = None
            def __init__(self,*args,**kwargs):
                Browser.received=kwargs
                self.site=args[3]
                self.page=SimpleNamespace(url=G)
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def visit(self, url, timeout=0): self.page.url=url;return 200
            def build_numbers(self,url):return {2}
            def _history_result(self,url,n):return 'SUCCESS'
            def _heading_result(self,n):return 'SUCCESS'
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp,patch.object(diagnose,'DATA',Path(tmp)):
            r=diagnose.run_diagnosis(G,config=C,factory=Browser,visible=False)
        self.assertEqual(r['site'],'gmx');self.assertEqual(r['verdict'],'PASS')
        self.assertTrue(Browser.received['visible']);self.assertTrue(Browser.received['manual_login'])


class DiagnosticApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.token='unit-test-diagnostics-csrf'
        cls.httpd=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler(cls.token))
        cls.port=cls.httpd.server_port
        cls.thread=threading.Thread(target=cls.httpd.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown();cls.httpd.server_close();cls.thread.join(timeout=3)
    def request(self, method,path,body=None,csrf=True):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        headers={'Host':f'127.0.0.1:{self.port}','Origin':f'http://127.0.0.1:{self.port}'}
        if method=='POST':
            headers['Content-Type']='application/json'
            if csrf:headers['X-CSRF-Token']=self.token
        try:
            c.request(method,path,body=json.dumps(body).encode() if body is not None else None,headers=headers)
            r=c.getresponse();return r.status,json.loads(r.read())
        finally:c.close()
    def test_plugin_and_gmx_live_traces(self):
        def fake_diag(url,*,number=None,expected='SUCCESS',visible=False,config=None,emit=None,expected_artifacts=None):
            site='plugin' if 'release_1_3_' in url else 'gmx'
            emit('[BROWSER] visible Chrome started');emit('[AUTH] manual SSO permitted')
            emit('[RESULT] Build #2 SUCCESS')
            return {'verdict':'PASS','actual':'SUCCESS','expected':expected,'site':site,'build_number':2}
        with patch.object(diagnose,'run_diagnosis',side_effect=fake_diag) as runner,patch.object(web,'create_build') as create:
            for site,url in [('plugin',P),('gmx',G)]:
                body={'url':url,'site':site,'build_number':2}
                if site=='plugin':body['expected_artifacts']={'version':'1.3.0','journey_sha256':'a'*64,'domainmodel_sha256':'b'*64}
                status,r=self.request('POST','/api/diagnose/start',body)
                self.assertEqual(status,202,r)
                for i in range(40):
                    status,s=self.request('GET','/api/diagnose/status/'+r['id'])
                    if s['status']=='DONE':break
                    time.sleep(.01)
                self.assertEqual(s['status'],'DONE',s)
                self.assertEqual(s['report']['verdict'],'PASS')
                self.assertIn('manual SSO permitted','\n'.join(s['logs']))
            self.assertEqual(runner.call_count,2)
            self.assertEqual(runner.call_args_list[0].kwargs['expected_artifacts'],{'version':'1.3.0','journey_sha256':'a'*64,'domainmodel_sha256':'b'*64})
            self.assertTrue(all(c.kwargs['visible'] for c in runner.call_args_list))
            create.assert_not_called()

    def test_site_url_mismatch_and_csrf_protected(self):
        code,data=self.request('POST','/api/diagnose/start',{'url':P,'site':'gmx'})
        self.assertEqual(code,400,data)
        code,data=self.request('POST','/api/diagnose/start',{'url':P,'site':'plugin'},csrf=False)
        self.assertEqual(code,403,data)

    def test_web_has_two_independent_entries_and_poll(self):
        html=(web.STATIC/'index.html').read_text()
        js=(web.STATIC/'app.js').read_text()
        for name in ['diagnose-form','diagnose-gmx-form']:
            self.assertIn('id="'+name+'"',html)
            self.assertIn("$('"+name+"').addEventListener('submit'",js)
        self.assertIn('diagnose/status/',js)

if __name__=='__main__':unittest.main()
