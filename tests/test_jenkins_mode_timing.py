"""Same async-script fixture; actual headed test skips when no display is available."""
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from playwright.sync_api import sync_playwright
from appbuilder.jenkins import JenkinsBrowser

class ModeTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        root=Path(cls.tmp.name)
        cls.cert,cls.key=root/'cert',root/'key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(cls.key),
                        '-out',str(cls.cert),'-days','1','-subj','/CN=localhost'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.engine=sync_playwright().start()
    @classmethod
    def tearDownClass(cls):cls.engine.stop();cls.tmp.cleanup()
    def setUp(self):
        self.posts=[]
        posts=self.posts
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/bind.js':
                    time.sleep(.5)
                    body=b'document.querySelector("form").addEventListener("submit",()=>fetch("/job/demo/build",{method:"POST"}));'
                    kind='application/javascript'
                else:
                    body=b'''<a href="#params" onclick="document.querySelector('form').hidden=false">Build with Parameters</a>
                    <form hidden method="post" action="/job/demo/build" onsubmit="event.preventDefault()"><button type="submit">Build</button></form>
                    <script async src="/bind.js"></script>''';kind='text/html'
                self.send_response(200);self.send_header('Content-Type',kind);self.end_headers();self.wfile.write(body)
            def do_POST(self):
                posts.append(self.path);self.send_response(201);self.send_header('Location','/queue/item/3/');self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(self.cert,self.key)
        self.server.socket=tls.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.job=f'https://127.0.0.1:{self.server.server_port}/job/demo/'
    def worker(self,headless=True,slow_mo=0):
        browser=self.engine.chromium.launch(executable_path='/usr/bin/chromium',headless=headless,slow_mo=slow_mo,args=['--no-sandbox'])
        self.addCleanup(browser.close)
        context=browser.new_context(ignore_https_errors=True);self.addCleanup(context.close)
        worker=JenkinsBrowser.__new__(JenkinsBrowser);worker.page=context.new_page();worker.site='plugin';worker.visible=not headless
        worker.visit=lambda url:worker.page.goto(url,wait_until='domcontentloaded')
        return worker
    def legacy_sequence(self,worker):
        worker.visit(self.job)
        worker._safe_click('Build with Parameters','link')
        worker._safe_click('Build','button')
        worker.page.wait_for_timeout(1100)
    def test_fast_click_before_async_binding_reproduces_no_post(self):
        self.legacy_sequence(self.worker())
        self.assertEqual(self.posts,[])
    def test_visible_slow_mo_setting_masks_same_fixture_race(self):
        # This isolates the production timing difference, not a headed claim.
        self.legacy_sequence(self.worker(slow_mo=300))
        self.assertEqual(self.posts,['/job/demo/build'])
    def test_headless_ready_boundary_submits_once_without_slow_mo(self):
        worker=self.worker();receipts=[];worker.build_dispatch_checkpoint=lambda **kw:receipts.append(kw)
        worker.request_build(self.job,with_parameters=True)
        self.assertEqual(self.posts,['/job/demo/build'])
        self.assertEqual(receipts[-1]['state'],'ACCEPTED')
    @unittest.skipUnless(os.environ.get('DISPLAY'),'Actual headed Chromium requires X server/DISPLAY; unavailable here')
    def test_actual_headed_same_fixture_submits_once(self):
        worker=self.worker(headless=False,slow_mo=300)
        worker.request_build(self.job,with_parameters=True)
        self.assertEqual(self.posts,['/job/demo/build'])

    @unittest.skipUnless(os.environ.get('DISPLAY'),'Actual headed Chromium requires X server/DISPLAY; unavailable here')
    def test_actual_headed_without_slow_mo_exposes_same_race(self):
        self.legacy_sequence(self.worker(headless=False,slow_mo=0))
        self.assertEqual(self.posts,[])
