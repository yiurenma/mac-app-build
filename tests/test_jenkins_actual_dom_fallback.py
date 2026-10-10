"""Regression reproductions of the user's UNKNOWN #2 with new Jenkins 'Builds' sidebar."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from appbuilder.diagnose import run_diagnosis
from appbuilder.config import load_config
from types import SimpleNamespace
from playwright.sync_api import sync_playwright
from appbuilder.jenkins import JenkinsBrowser, status_from_icon_colors

JOB='https://ci.corp/job/p/'

# A modern Jenkins theme may use 'Builds', without #buildHistory/.build-row.
HTML='''<!doctype html><html><head><style>
.success-tick {width:20px;height:20px;color:rgb(48, 176, 88)}
.failed-tick {width:20px;height:20px;color:rgb(217, 51, 60)}
.running-icon {width:20px;height:20px;color:rgb(128, 128, 128)}
</style></head><body>
<aside id="jenkins-builds-card"><h3>Builds</h3>
  <div class="jenkins-modern-row"><span><svg class="success-tick"><circle r="4"/></svg></span><a href="https://ci.corp/job/p/1/">#1</a></div>
  <div class="jenkins-modern-row"><span><svg class="{status}"><circle r="4"/></svg></span><a href="https://ci.corp/job/p/2/">#2</a></div>
</aside><h1>release/1.3.202610101332</h1>
</body></html>'''

class RealThemeUnknownRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=sync_playwright().start()
        cls.browser=cls.engine.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.engine.stop()
    def setUp(self):
        self.page=self.browser.new_page()
        self.obj=SimpleNamespace(page=self.page,site='plugin')
    def tearDown(self):self.page.close()
    def test_modern_builds_sidebar_green_tick_of_exact_2(self):
        self.page.set_content(HTML.replace("{status}", 'success-tick'))
        self.assertEqual(JenkinsBrowser._history_result(self.obj,JOB,2),'SUCCESS')
        self.assertEqual(self.obj.last_history_probe['targetLinks'],1)
        self.assertEqual(len(self.obj.last_history_probe['candidates']),1)
    def test_modern_sidebar_gray_running_2_still_unknown(self):
        self.page.set_content(HTML.replace("{status}", 'running-icon'))
        self.assertIsNone(JenkinsBrowser._history_result(self.obj,JOB,2))
    def test_modern_sidebar_red_2_after_green_1_not_success(self):
        self.page.set_content(HTML.replace("{status}", 'failed-tick'))
        self.assertEqual(JenkinsBrowser._history_result(self.obj,JOB,2),'FAILURE')
    def test_progress_bar_greens_do_not_count(self):
        html=HTML.replace("{status}", 'running-icon').replace('<svg class="running-icon"><circle r="4"/></svg>','<div style="background:rgb(48,176,88);width:70px;height:4px"></div>')
        self.page.set_content(html)
        self.assertIsNone(JenkinsBrowser._history_result(self.obj,JOB,2))
    def test_page_with_multiple_build_links_never_uses_parent_1_icon(self):
        html='''<aside><svg style="color:green;width:20px;height:20px"></svg>
<a href="https://ci.corp/job/p/1/">#1</a><a href="https://ci.corp/job/p/2/">#2</a></aside>'''
        self.page.set_content(html)
        self.assertIsNone(JenkinsBrowser._history_result(self.obj,JOB,2))

class DiagnosticEvidence(unittest.TestCase):
    def test_unknown_explains_dom_mismatch_and_html_console_without_build(self):
        config=load_config()
        job=config['jenkins']['plugin_parent']+'release_1_3_202610101332/'
        class Response:
            status=200
            url=job+'2/consoleText'
            headers={'content-type':'text/html; charset=utf-8'}
            def text(self):return '<html>SSO gateway has no log</html>'
        class Browser:
            def __init__(self,*args,**kwargs):
                self.page=SimpleNamespace(url=job,screenshot=lambda **kwargs:None)
                self.context=SimpleNamespace(request=SimpleNamespace(get=lambda *args,**kwargs:Response()))
                self.last_history_probe={'targetLinks':1,'pageBuildLinks':2,'candidates':[]}
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def visit(self,url,timeout=0):self.page.url=url;return 200
            def build_numbers(self,url):return {1,2}
            def _history_result(self,job,n):return None
            def _heading_result(self,n):return None
        with tempfile.TemporaryDirectory() as tmp,patch('appbuilder.diagnose.DATA',Path(tmp)):
            report=run_diagnosis(job,config=config,factory=Browser,number=2)
        self.assertEqual(report['verdict'],'UNKNOWN')
        logs='\n'.join(report['logs'])
        self.assertIn('精确 Build #2 链接=1',logs)
        self.assertIn('type=text/html',logs)
        # Plugin diagnostics read the full log via read_full_console(), which rejects HTML
        # with its own message instead of the older GMX-only "HTTP 200 …" hint.
        self.assertIn('consoleText 返回 HTML，尚未取得原始构建日志',logs)
        self.assertIn('[SNAPSHOT]',logs)

class Colors(unittest.TestCase):
    def test_css_palette(self):
        self.assertEqual(status_from_icon_colors(['rgb(48, 176, 88)']),'SUCCESS')
        self.assertEqual(status_from_icon_colors(['rgb(217, 51, 60)']),'FAILURE')
        self.assertIsNone(status_from_icon_colors(['rgb(128, 128, 128)']))
        self.assertIsNone(status_from_icon_colors(['rgb(48, 176, 88)','rgb(217, 51, 60)']))

if __name__=='__main__':unittest.main()
