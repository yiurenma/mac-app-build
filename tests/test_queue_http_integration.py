"""Real local HTTPS Jenkins fixture; production POST, queue GET and monitoring.

No request interception and no mocked queue resolver. TLS trust is disabled only
for this fixture's self-signed localhost certificate.
"""
import json
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from appbuilder import jenkins
import test_nexus_lock as nexus_fixture


class QueueHTTPFixture:
    headed = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key'),
                        '-out',str(root/'cert'),'-days','1','-subj','/CN=localhost'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.engine = sync_playwright().start()
        cls.browser = cls.engine.chromium.launch(executable_path='/usr/bin/chromium',
            headless=not cls.headed, args=['--no-sandbox'])
        cls.cert, cls.key = root/'cert', root/'key'

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.engine.stop(); cls.tmp.cleanup()

    def setUp(self):
        self.events=[]; self.mode='normal'; self.reads=0; self.payload_override=None
        self.plugin_chain=False; self.callback_result='SUCCESS'; self.queue_location='/jenkins/queue/item/91/'
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,status,body,ctype='text/html',headers=None):
                raw=body.encode();self.send_response(status)
                self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)))
                for key,value in (headers or {}).items():self.send_header(key,value)
                self.end_headers()
                try:self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError,ssl.SSLError):pass
            def do_POST(self):
                owner.events.append(('POST',self.path,self.headers.get('Cookie','')))
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                self.reply(201,'accepted',headers={'Location':owner.queue_location})
            def do_GET(self):
                path=urlsplit(self.path).path
                owner.events.append(('GET',self.path,self.headers.get('Cookie','')))
                if path=='/jenkins/queue/item/91/api/json':
                    owner.reads+=1
                    if 'session=fixture' not in self.headers.get('Cookie',''):
                        self.reply(403,'denied');return
                    if owner.mode in ('401','403','404','410','500'):
                        self.reply(int(owner.mode),'error');return
                    if owner.mode=='redirect':
                        self.reply(302,'',headers={'Location':owner.origin+'/should-not-follow'});return
                    if owner.mode=='html':self.reply(200,'<html>login</html>');return
                    if owner.mode=='json':self.reply(200,'{','application/json');return
                    if owner.mode=='network':self.connection.close();return
                    if owner.mode=='slow':time.sleep(.2)
                    payload=dict(id=91,task={'url':owner.job},cancelled=owner.mode=='cancelled')
                    if owner.mode!='waiting' and owner.reads>1:
                        payload['executable']={'number':102,'url':owner.job+'102/'}
                    if owner.payload_override is not None:payload=owner.payload_override
                    self.reply(200,json.dumps(payload),'application/json');return
                if path.endswith('/build') and urlsplit(self.path).query:
                    self.reply(200,'<form method="post" action="build"><button type="submit">Build</button></form>');return
                if path.endswith('/consoleText') and owner.plugin_chain:
                    if path == urlsplit(owner.job+'103/consoleText').path:
                        self.reply(200,nexus_fixture.console(),'text/plain');return
                    self.reply(200,'Manual Trigger: build will be created soon\nFinished: SUCCESS\n','text/plain');return
                if owner.plugin_chain:
                    cards='<div><a href="'+owner.job+'101/">#101</a><svg class="icon-success" width="20" height="20"></svg>Manual Trigger</div>'
                    if owner.reads>1:
                        cards+='<div><a href="'+owner.job+'102/">#102</a><svg class="icon-success" width="20" height="20"></svg>Manual Trigger: build will be created soon</div>'
                        # The other user's Callback appears first; only Source #102 belongs to this trigger.
                        cards+='<div><a href="'+owner.job+'104/">#104</a> Manual Callback ← Source #101<svg class="icon-success" width="20" height="20"></svg></div>'
                        icon='icon-success' if owner.callback_result=='SUCCESS' else 'icon-failure'
                        cards+='<div><a href="'+owner.job+'103/">#103</a> Manual Callback ← Source #102<svg class="'+icon+'" width="20" height="20"></svg></div>'
                    self.reply(200,'<a href="build?delay=0sec">Build with Parameters</a>'+cards,
                               headers={'Set-Cookie':'session=fixture; Path=/; Secure; HttpOnly'});return
                # Another user's #101 exists before our queue becomes #102.
                cards='<div><a href="'+owner.job+'101/">#101</a><svg class="icon-failure"></svg></div>'
                if owner.reads>1:cards+='<div><a href="'+owner.job+'102/">#102</a><svg class="icon-success" width="20" height="20"></svg></div>'
                self.reply(200,'<a href="build?delay=0sec">Build with Parameters</a>'+cards,
                           headers={'Set-Cookie':'session=fixture; Path=/; Secure; HttpOnly'})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(self.cert,self.key)
        self.server.socket=tls.wrap_socket(self.server.socket,server_side=True)
        self.origin=f'https://127.0.0.1:{self.server.server_port}'
        self.job=self.origin+'/jenkins/job/example/'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        self.state_dir=tempfile.TemporaryDirectory();self.addCleanup(self.state_dir.cleanup)
        self.state_path=Path(self.state_dir.name)/'state.json'
        self.state_path.write_text(json.dumps({'runtime':{'dispatch_policy':'receipt-v1'}}))
        def read(task):return json.loads(self.state_path.read_text())
        def save(task,**fields):
            state=read(task);state['runtime'].update(fields);self.state_path.write_text(json.dumps(state))
        self.read=read
        for target,value in [('read_state',read),('patch_runtime',save)]:
            p=patch.object(jenkins,target,value);p.start();self.addCleanup(p.stop)
        self.context=self.browser.new_context(ignore_https_errors=True)
        self.addCleanup(self.context.close)
        self.new_worker()

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def new_worker(self):
        worker=jenkins.JenkinsBrowser.__new__(jenkins.JenkinsBrowser)
        worker.context=self.context;worker.page=self.context.new_page();worker.page.set_default_timeout(2000)
        worker.site='gmx';worker.visible=self.headed;worker.manual_login=False;worker.credentials=None
        # Use the Plugin parameter form path without GMX-specific parameter filling;
        # formal_build(site='gmx') retains the normal direct result monitor.
        worker.site='plugin';worker.conf={'queue_timeout_minutes':.02,'build_timeout_minutes':.05}
        worker.poll=.01;worker.result_poll=2
        self.worker=worker

    def run_build(self):return jenkins.formal_build(self.worker,self.job,'task','gmx')
    def posts(self):return [e for e in self.events if e[0]=='POST']

    def test_queue_wait_then_exact_build_and_existing_monitor_despite_other_user(self):
        number,_=self.run_build()
        self.assertEqual(number,102);self.assertEqual(len(self.posts()),1)
        rt=self.read('task')['runtime'];self.assertEqual(rt['gmx_formal_result'],'SUCCESS')
        self.assertEqual(rt['gmx_formal_identity']['queue_id'],91)
        self.assertEqual(rt['gmx_formal_identity']['build_url'],self.job+'102/')
        self.assertGreaterEqual(self.reads,2)
        api=[e for e in self.events if '/api/json' in e[1]]
        self.assertTrue(all(e[0]=='GET' and 'session=fixture' in e[2] for e in api))
        self.new_worker();self.assertEqual(self.run_build()[0],102)
        self.assertEqual(len(self.posts()),1)

    def test_resume_after_queue_timeout_uses_persisted_receipt_not_second_post(self):
        self.mode='waiting';self.worker.conf['queue_timeout_minutes']=.001
        with self.assertRaises(jenkins.NeedsAttention):self.run_build()
        self.assertEqual(self.read('task')['runtime']['gmx_formal_dispatch']['queue_id'],91)
        self.new_worker();self.mode='normal'
        self.assertEqual(self.run_build()[0],102);self.assertEqual(len(self.posts()),1)

    def test_cancelled_disappeared_and_auth_errors_preserve_receipt(self):
        for mode in ('cancelled','401','403','404','410','500','html','json','redirect','network'):
            with self.subTest(mode=mode):
                self.mode=mode
                with self.assertRaises(jenkins.NeedsAttention):self.run_build()
                self.assertEqual(len(self.posts()),1)
                self.assertNotIn('gmx_formal_number',self.read('task')['runtime'])
        self.assertFalse(any('/should-not-follow' in e[1] for e in self.events))
        self.mode='normal';self.new_worker()
        self.assertEqual(self.run_build()[0],102);self.assertEqual(len(self.posts()),1)

    def plugin_result(self, resume_between=False):
        trigger,_=jenkins.formal_build(self.worker,self.job,'task','plugin')
        self.assertEqual(trigger,102)
        if resume_between:
            self.new_worker()
            self.assertEqual(jenkins.formal_build(self.worker,self.job,'task','plugin')[0],102)
        return jenkins.plugin_artifact_info(self.worker,self.job,trigger,'task')

    def test_plugin_trigger_to_own_callback_to_artifacts_uses_production_chain(self):
        self.plugin_chain=True
        version,hashes,artifact_build=self.plugin_result()
        self.assertEqual((version,hashes,artifact_build),(nexus_fixture.VERSION,nexus_fixture.HASHES,103))
        rt=self.read('task')['runtime']
        self.assertEqual(rt['plugin_formal_number'],102)
        self.assertEqual(rt['plugin_callback_number'],103)
        self.assertEqual(rt['plugin_artifact_result'],'SUCCESS')
        self.assertEqual(len(self.posts()),1)
        self.assertFalse(any('/104/consoleText' in e[1] for e in self.events))

    def test_plugin_green_trigger_failed_callback_never_returns_artifacts(self):
        self.plugin_chain=True;self.callback_result='FAILURE'
        with self.assertRaisesRegex(RuntimeError,'Callback #103 失败'):
            self.plugin_result()
        rt=self.read('task')['runtime']
        self.assertEqual(rt['plugin_formal_result'],'SUCCESS')
        self.assertEqual(rt['plugin_callback_number'],103)
        self.assertNotIn('plugin_artifact_result',rt)
        self.assertEqual(len(self.posts()),1)

    def test_plugin_resume_between_trigger_and_callback_preserves_origin(self):
        self.plugin_chain=True
        self.assertEqual(self.plugin_result(resume_between=True)[2],103)
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.read('task')['runtime']['plugin_formal_identity']['queue_id'],91)

    def test_receipt_from_another_context_is_not_queried(self):
        self.queue_location='/another-jenkins/queue/item/91/'
        with self.assertRaises(jenkins.NeedsAttention):self.run_build()
        with self.assertRaises(jenkins.NeedsAttention):self.run_build()
        self.assertEqual(self.reads,0)
        self.assertEqual(len(self.posts()),1)

    def test_nested_job_retains_controller_context(self):
        self.job=self.origin+'/jenkins/job/folder/job/example/'
        self.assertEqual(self.run_build()[0],102)
        receipt=self.read('task')['runtime']['gmx_formal_dispatch']
        self.assertEqual(receipt['queue_url'],self.origin+'/jenkins/queue/item/91/')
        self.assertEqual(len(self.posts()),1)

    def test_request_timeout_preserves_receipt(self):
        self.mode='slow';self.worker.conf['queue_timeout_minutes']=.001
        with self.assertRaises(jenkins.NeedsAttention):self.run_build()
        self.assertEqual(len(self.posts()),1)

    def test_invalid_identity_and_executable_never_adopt_other_build(self):
        base=dict(id=91,task={'url':self.job},cancelled=False,
                  executable={'number':102,'url':self.job+'102/'})
        invalid=[{**base,'id':92},{**base,'id':True},{**base,'task':{'url':self.job+'other/'}},
                 {**base,'cancelled':'false'}]
        for url in (self.origin+'/jenkins/job/other/102/', 'https://other.invalid/job/example/102/',
                    self.job+'102/?x=1',self.job+'102/#fragment',self.job+'../102/',
                    self.job+'%31%30%32/',self.job+'0/',self.job.replace('https://','http://')+'102/'):
            invalid.append({**base,'executable':{'number':102,'url':url}})
        invalid.extend([{**base,'executable':{'number':101,'url':self.job+'102/'}},
                        {**base,'executable':{'number':True,'url':self.job+'1/'}},
                        {**base,'executable':'bad'}])
        for item in invalid:
            self.payload_override=item
            with self.assertRaises(jenkins.NeedsAttention):self.run_build()
            self.assertEqual(len(self.posts()),1)
            self.assertNotIn('gmx_formal_number',self.read('task')['runtime'])


class HeadlessQueueHTTPTests(QueueHTTPFixture,unittest.TestCase):pass
class HeadedQueueHTTPTests(QueueHTTPFixture,unittest.TestCase):headed=True
