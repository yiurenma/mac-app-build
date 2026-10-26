"""Real HTTP handler and Chromium UI; only task DB fixture is substituted."""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from appbuilder import web

A='202610141200-abcdef';B='202610141201-abcdef'
class RuntimeBrowserTests(unittest.TestCase):
    def test_visible_and_hidden_snapshots_and_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for task_id in (A,B):
                folder=root/'runs'/task_id;folder.mkdir(parents=True)
                (folder/'state.json').write_text(json.dumps({'completed':['PLUGIN_PREPARE'],'runtime':{'plugin_formal_number':102,'plugin_callback_number':103,'password':'DO_NOT_EXPOSE','marker':task_id,'step_errors':{'PLUGIN_JENKINS':'<img src=x onerror=alert(1)> Callback failed'}}}))
            def lookup(task_id):
                if task_id==A:time.sleep(.15)
                return {'id':task_id,'status':'FAILED','stage':'PLUGIN_JENKINS','created':'2026-10-14'} if task_id in (A,B) else None
            with patch.object(web,'DATA',root),patch.object(web,'task',side_effect=lookup):
                server=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler('test-token'))
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with sync_playwright() as p:
                        for visible in (False,True):
                            with self.subTest(visible=visible):
                                browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=not visible,args=['--no-sandbox'])
                                context=browser.new_context(viewport={'width':1440,'height':1050},permissions=['clipboard-read','clipboard-write'])
                                page=context.new_page();page.route('**/api/bootstrap',lambda route:route.fulfill(status=400,json={'error':'fixture'}))
                                base=f'http://127.0.0.1:{server.server_port}'
                                page.goto(base)
                                page.evaluate('''ids=>{state.tasks=ids.map(id=>({id,status:'FAILED',stage:'PLUGIN_JENKINS',created:'2026-10-14',project:'Example',country:'UK',plugin_version:'1.3.1'}));state.selected=ids[0];renderTasks();}''',[A,B])
                                page.locator('#tasks-body button').nth(1).click()
                                page.locator("#runtime-copy:not([disabled])").wait_for(state="visible")
                                self.assertIn(B,page.locator('#runtime-task').inner_text());self.assertEqual(page.evaluate('state.selected'),A)
                                self.assertEqual(page.locator('#runtime-dialog img').count(),0)
                                page.locator('#runtime-json-tab').click();text=page.locator('#runtime-json').inner_text()
                                self.assertNotIn('DO_NOT_EXPOSE',text);self.assertIn('[已隐藏]',text)
                                page.locator('#runtime-copy').click();self.assertEqual(page.evaluate('navigator.clipboard.readText()'),text)
                                page.evaluate('(ids)=>{openRuntimeData(ids[0]);openRuntimeData(ids[1]);}',[A,B]);page.wait_for_timeout(400)
                                self.assertIn(B,page.locator('#runtime-task').inner_text())
                                page.set_viewport_size({'width':390,'height':844});box=page.locator('#runtime-dialog').bounding_box();self.assertLessEqual(box['width'],390)
                                page.keyboard.press('Escape');self.assertFalse(page.locator('#runtime-dialog').is_visible())
                                for path in [f'/api/tasks/{A}/runtime-data?path=/etc/passwd','/api/tasks/%2e%2e/runtime-data','/api/tasks/202610141202-abcdef/runtime-data']:
                                    self.assertIn(context.request.get(base+path).status,(400,404))
                                context.close();browser.close()
                finally:server.shutdown();server.server_close();thread.join()
