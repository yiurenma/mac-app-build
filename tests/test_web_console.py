"""Local Web Console: real loopback HTTP tests without Jenkins/network/build actions."""
from __future__ import annotations
import contextlib
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock

from appbuilder import web
from appbuilder.cli import args_parser

TOKEN = 'csrf-test-only-strong-token'


class WebHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer((web.HOST, 0), web.make_handler(TOKEN))
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        h = {'Host': f'{web.HOST}:{self.port}'}
        h.update(headers or {})
        conn = http.client.HTTPConnection(web.HOST, self.port, timeout=5)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                         headers=h)
            res = conn.getresponse()
            data = res.read()
            return res.status, dict(res.getheaders()), data
        finally:
            conn.close()

    def test_page_assets_and_security_headers(self):
        for path, marker, mime in [('/', b'Mac APP Builder', 'text/html'),
                                   ('/app.css', b'.sidebar', 'text/css'),
                                   ('/app.js', b'submitBuild', 'application/javascript')]:
            status, h, body = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn(marker, body)
            self.assertIn(mime, h['Content-Type'])
            self.assertEqual(h['X-Frame-Options'], 'DENY')
            self.assertEqual(h['Cache-Control'], 'no-store')
            self.assertIn("script-src 'self'", h['Content-Security-Policy'])

    def test_wrong_host_rejected(self):
        code, _, data = self.request('GET', '/api/tasks', headers={'Host': 'evil.test'})
        self.assertEqual(code, 403)

    def test_nonlocal_origin_rejected_even_with_csrf(self):
        code, _, data = self.request('POST', '/api/tasks', body={}, headers={
            'Origin': 'https://evil.example', 'Content-Type': 'application/json',
            'X-CSRF-Token': TOKEN})
        self.assertEqual(code, 403)

    def test_missing_csrf_rejected(self):
        code, _, _ = self.request('POST', '/api/tasks', {}, {'Content-Type': 'application/json'})
        self.assertEqual(code, 403)

    def test_valid_csrf_and_origin_create_task(self):
        with patch.object(web, 'create_build', return_value={'id': '202610101234-abcdef'}) as fn:
            code, _, body = self.request('POST', '/api/tasks', {'plugin_branch':'develop'}, {
                'Origin': f'http://127.0.0.1:{self.port}',
                'Content-Type': 'application/json', 'X-CSRF-Token': TOKEN})
            self.assertEqual(code, 201)
            self.assertEqual(json.loads(body)['task']['id'], '202610101234-abcdef')
            fn.assert_called_once()

    def test_resume_route_dispatch(self):
        with patch.object(web, 'resume_build', return_value={'status':'RUNNING'}) as fn:
            code, _, body = self.request('POST', '/api/tasks/202610101234-abcdef/resume',
                     {'visible': True}, {'Content-Type':'application/json','X-CSRF-Token':TOKEN})
            self.assertEqual(code, 200)
            fn.assert_called_once_with('202610101234-abcdef', {'visible': True})

    def test_long_content_body_rejected(self):
        code, _, _ = self.request('POST', '/api/tasks', {'junk': 'x'*20000},
                                {'Content-Type':'application/json', 'X-CSRF-Token':TOKEN})
        self.assertEqual(code, 413)

    def test_get_endpoints_and_missing_route(self):
        with patch.object(web, 'task_list', return_value=[]):
            c, _, b = self.request('GET','/api/tasks')
            self.assertEqual(c,200)
            self.assertEqual(json.loads(b),{'tasks':[]})
        c, _, _ = self.request('GET','/nonsense')
        self.assertEqual(c,404)

    def test_invalid_offset_rejected(self):
        c, _, _ = self.request('GET','/api/tasks/202610101234-abcdef/log?offset=../../etc/passwd')
        self.assertEqual(c,400)

    def test_diagnosis_does_not_disclose_token(self):
        with patch.object(web, 'get_doctor', return_value={'ready':False,'messages':['token missing']}):
            c, _, b = self.request('GET','/api/doctor')
        self.assertEqual(json.loads(b)['ready'],False)

    def test_bootstrap_exposes_csrf_and_nonsecret_defaults(self):
        with patch.object(web, 'get_doctor', return_value={'ready':True,'messages': []}):
            c, _, b=self.request('GET','/api/bootstrap')
        self.assertEqual(c,200)
        data=json.loads(b)
        self.assertEqual(data['csrf'],TOKEN)
        self.assertIn('max_parallel',data['config'])


class WebLogicTests(unittest.TestCase):
    def test_web_cli_flags(self):
        args=args_parser().parse_args(['web','--port','9876','--no-open','--update'])
        self.assertEqual(args.command,'web')
        self.assertTrue(args.no_open)
        self.assertTrue(args.update)

    def test_public_task_does_not_send_password_or_repo_token(self):
        row={'id':'202610101234-abcdef','created':'2026-10-10','params':json.dumps({
            'project':'PayByBank','country':'UK','plugin_version_input':'1.3',
            'password':'do_not_leak','token':'do_not_leak','plugin_branch':'release/abc'}),
            'stage':'PLUGIN_JENKINS','status':'FAILED','note':'Test', 'pid':None}
        public=web.public_task(row)
        self.assertNotIn('password', public)
        self.assertNotIn('token', public)
        self.assertNotIn('do_not_leak',json.dumps(public))
        self.assertEqual(public['plugin_version'],'1.3')

    def test_build_creates_task_and_uses_existing_keychain(self):
        payload={'plugin_branch':'develop','gmx_branch':'develop','plugin_version':'1.3',
                 'project_name':'PayByBank','country':'UK', 'visible':True,'verbose':True}
        from appbuilder import cli
        with patch.object(cli,'doctor_check',return_value=True),\
             patch.object(web,'token_or_error',return_value='dummy'),\
             patch.object(web,'active_count',return_value=0),\
             patch.object(web,'create_task',return_value={'id':'202610101234-abcdef'}) as create,\
             patch.object(web,'task',return_value={
               'id':'202610101234-abcdef','created':'now','params':json.dumps({'project':'PayByBank'}),
               'status':'QUEUED','pid':None,'stage':'INIT','note':''}),\
             patch.object(cli,'spawn') as spawn:
            created=web.create_build(payload)
        self.assertEqual(created['id'],'202610101234-abcdef')
        self.assertTrue(create.call_args.args[1]['visible'])
        self.assertTrue(create.call_args.args[1]['verbose'])
        spawn.assert_called_once_with('202610101234-abcdef',wait=False)

    def test_invalid_inputs_cannot_spawn_build(self):
        with patch('appbuilder.cli.spawn') as spawn:
            with self.assertRaises(ValueError):
                web.create_build({'plugin_branch':'develop','gmx_branch':'develop',
                                  'plugin_version':'1.3','project_name':'PayByBank',
                                  'country':'UK','jenkins_user':'foo'})
            spawn.assert_not_called()

    def test_resume_refuses_active_worker(self):
        with patch.object(web,'task',return_value={'status':'RUNNING','pid':432}),\
             patch('appbuilder.cli.alive',return_value=True),\
             patch('appbuilder.cli.spawn') as spawn:
            with self.assertRaisesRegex(ValueError,'仍在运行'):
                web.resume_build('202610101234-abcdef',{'visible':True})
            spawn.assert_not_called()

    def test_resume_does_not_create_another_task(self):
        row={'id':'202610101234-abcdef','created':'now','status':'FAILED','pid':None,
             'stage':'PLUGIN_JENKINS','note':'failed','params':'{}'}
        from appbuilder import cli
        with patch.object(web,'task',return_value=row),\
             patch.object(web,'active_count',return_value=0),\
             patch.object(cli,'doctor_check',return_value=True),\
             patch.object(web,'token_or_error',return_value='dummy'),\
             patch.object(web,'update_task_params') as update,\
             patch.object(web,'create_task') as create,\
             patch.object(cli,'spawn') as spawn:
            result=web.resume_build(row['id'],{'visible':True,'verbose':True})
            create.assert_not_called()
            update.assert_called_once()
            spawn.assert_called_once()

    def test_log_tail_and_no_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)
            (path/'run.log').write_text('hello\n'[0:]+('[TRACE] progress\n'*30000),encoding='utf-8')
            with patch.object(web,'task',return_value={'id':'202610101234-abcdef'}),\
                 patch.object(web,'task_folder',return_value=path):
                latest=web.read_log('202610101234-abcdef',None)
                self.assertLessEqual(len(latest['lines'].encode()),web.MAX_LOG_BYTES+100)
                self.assertEqual(latest['offset'],(path/'run.log').stat().st_size)
                idle=web.read_log('202610101234-abcdef',latest['offset'])
                self.assertEqual(idle['lines'],'')
                with self.assertRaises(ValueError):
                    web.read_log('../etc/passwd',None)

    def test_screen_listing_only_regular_png(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=Path(temp)
            (parent/'screenshots').mkdir()
            (parent/'screenshots'/'plugin-error.png').write_bytes(b'png')
            (parent/'screenshots'/'secret.txt').write_text('secret')
            (parent/'screenshots'/'escape.png').symlink_to(parent/'screenshots'/'secret.txt')
            with patch.object(web,'task',return_value={'id':'202610101234-abcdef'}),\
                 patch.object(web,'task_folder',return_value=parent):
                self.assertEqual(web.get_screenshots('202610101234-abcdef'),['plugin-error.png'])

    def test_port_and_localhost_only(self):
        with self.assertRaises(ValueError):
            web.serve(80,open_browser=False)
        self.assertEqual(web.HOST,'127.0.0.1')

    def test_static_js_no_remote_analytics_or_inline_password(self):
        html=(web.STATIC/'index.html').read_text()
        js=(web.STATIC/'app.js').read_text()
        self.assertNotIn('https://cdn.',html)
        self.assertNotIn('<script src="https://',html)
        self.assertIn('textContent',js)
        self.assertNotIn('localStorage',js)
        self.assertNotIn('innerHTML',js)
        self.assertIn('type="password"',html)

if __name__=='__main__':
    unittest.main()
