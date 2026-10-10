"""v0.6.1: duplicate-start prevention and no-network/offline safe boot."""
import contextlib
import http.server
import io
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import launcher, web


class LauncherTests(unittest.TestCase):
    def test_detect_running_real_console(self):
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),web.make_handler('testtoken'))
        t=threading.Thread(target=server.serve_forever,daemon=True)
        t.start()
        try:
            self.assertEqual(launcher.probe(server.server_port),'running')
            with patch.object(launcher.webbrowser,'open') as browser:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launcher.main(['--port',str(server.server_port)]),0)
                browser.assert_called_once_with(f'http://127.0.0.1:{server.server_port}')
        finally:
            server.shutdown();server.server_close();t.join()

    def test_detect_other_service_no_redirect(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location','https://example.com/steal')
                self.end_headers()
            def log_message(self, *args): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            self.assertEqual(launcher.probe(server.server_port),'occupied')
            with patch.object(launcher.webbrowser,'open') as browser:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(launcher.main(['--port',str(server.server_port)]),2)
                browser.assert_not_called()
        finally:
            server.shutdown();server.server_close();t.join()

    def test_free_port(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port=s.getsockname()[1]
        self.assertEqual(launcher.probe(port),'free')
        with patch.object(launcher.webbrowser,'open') as browser:
            self.assertEqual(launcher.main(['--port',str(port),'--no-open']),1)
            browser.assert_not_called()

    def test_invalid_port(self):
        with self.assertRaises(ValueError): launcher.probe(22)

    def test_health_endpoint_exposes_no_secrets(self):
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),web.make_handler('testtoken'))
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            c=http.client.HTTPConnection('127.0.0.1', server.server_port,timeout=3)
            c.request('GET','/api/health',headers={'Host':f'127.0.0.1:{server.server_port}'})
            r=c.getresponse();import json
            data=json.loads(r.read());c.close()
            self.assertEqual(data['app'],'mac-app-builder')
            self.assertEqual(data['service'],'web-console')
            self.assertEqual(data['version'],'0.7.5')
            self.assertNotIn('csrf',data)
        finally:
            server.shutdown();server.server_close();t.join()


class JenkinsLinkTests(unittest.TestCase):
    def row(self, *, plugin_url='https://jenkins.example.corp/job/plugin/', gmx_url='https://jenkins.example.corp/job/gmx/'):
        import json
        return {'id':'202610101234-abcdef','created':'2026-10-10T12:34:00',
                'status':'RUNNING','pid':None,'stage':'PLUGIN_JENKINS','note':'',
                'params':json.dumps({'project':'PayByBank','plugin_job':plugin_url,'gmx_job':gmx_url,
                                     'jenkins_password':'DO_NOT_EXPOSE','github_token':'DO_NOT_EXPOSE'})}

    def test_links_show_trigger_and_callback_separately(self):
        with patch('appbuilder.store.read_state',return_value={'runtime':{
                'plugin_formal_number':1,'plugin_callback_number':2,
                'gmx_formal_number':7,'token':'SHOULD_NOT_LEAK'}}):
            result=web.public_task(self.row())
        actual={link['label']:link['url'] for link in result['jenkins_links']}
        self.assertEqual(actual['Plugin Build #1'],'https://jenkins.example.corp/job/plugin/1/')
        self.assertEqual(actual['Plugin Callback #2'],'https://jenkins.example.corp/job/plugin/2/')
        self.assertEqual(actual['GMX Build #7'],'https://jenkins.example.corp/job/gmx/7/')
        import json
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(result))
        self.assertNotIn('SHOULD_NOT_LEAK',json.dumps(result))

    def test_links_refuse_query_credentials_or_http_scheme(self):
        for url in ['javascript:alert(1)', 'http://jenkins.example/job/',
                    'https://u:password@jenkins.example/job/',
                    'https://jenkins.example/job/?auth=secret']:
            with self.subTest(url=url),patch('appbuilder.store.read_state',return_value={'runtime':{}}):
                links=web.public_task(self.row(plugin_url=url))['jenkins_links']
                self.assertFalse(any(x['label']=='Plugin Job' for x in links))

    def test_frontend_uses_safe_new_tab_links_and_live_hostname(self):
        content=(web.STATIC/'app.js').read_text()
        self.assertIn("a.rel='noopener noreferrer'",content)
        self.assertIn("target.protocol!=='https:'",content)
        self.assertIn('window.location.host',content)


class UpdateShellTests(unittest.TestCase):
    def execute(self, *, dirty=False, pull_fails=False, command='web'):
        """Execute real sh launcher with stubbed external apps, no network."""
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            wd=Path(tmp);(wd/'bin').mkdir();(wd/'.venv/bin').mkdir(parents=True)
            (wd/'app-build').write_bytes((root/'app-build').read_bytes())
            (wd/'app-build').chmod(0o755)
            # A minimal fake venv Python: checks deps and records the app entrypoint.
            p=wd/'.venv/bin/python'
            p.write_text('#!/bin/sh\ncase "$1" in\n  -c) exit 0;;\n  -m) echo "PYTHON_CALLED $*"; exit 0;;\nesac\n')
            p.chmod(0o755)
            git=wd/'bin/git'
            git.write_text('#!/bin/sh\ncase "$1" in\n rev-parse) exit 0;;\n status) '+('echo " M config.yaml"' if dirty else 'true')+'; exit 0;;\n pull) '+('exit 1' if pull_fails else 'exit 0')+';;\nesac\n')
            git.chmod(0o755)
            result=subprocess.run(['/bin/sh',str(wd/'app-build'),command,'--update'],
                cwd=wd,env={**os.environ,'PATH':str(wd/'bin')+':'+os.environ['PATH']},
                text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)
            return result

    def test_web_dirty_checkout_still_launches(self):
        r=self.execute(dirty=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('跳过自动更新',r.stdout)
        self.assertIn('PYTHON_CALLED -m appbuilder web --update',r.stdout)

    def test_web_offline_update_still_launches(self):
        r=self.execute(pull_fails=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('继续启动当前版本',r.stdout)

    def test_web_successful_update_launches(self):
        r=self.execute()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('更新完成',r.stdout)

    def test_cli_update_failure_keeps_strict_behavior(self):
        r=self.execute(dirty=True,command='run')
        self.assertNotEqual(r.returncode,0)
        self.assertNotIn('PYTHON_CALLED',r.stdout)

if __name__=='__main__': unittest.main()
