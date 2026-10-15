"""Real Chromium, fully routed offline auth readiness audit in both display modes.

Headed runs the exact same fixtures, or explicitly skips on no display.
A submitted click is not an authentication-success signal.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import Error, TimeoutError, sync_playwright
from appbuilder.auth import submit_recognized_login
from appbuilder.jenkins import JenkinsBrowser

HOST = 'https://jenkins.auth-fixture.invalid'
TARGET = HOST + '/job/release/job/feature%2Fready/42/'
FORM = '<form onsubmit="event.preventDefault(); window.submitted=true"><input name="j_username"><input name="j_password" type="password"><button type="submit">Sign in</button></form>'


class AuthReadinessFixtures:
    headless = True

    @classmethod
    def setUpClass(cls):
        cls.engine = sync_playwright().start()
        try:
            cls.browser = cls.engine.chromium.launch(executable_path='/usr/bin/chromium',
                                                     headless=cls.headless, args=['--no-sandbox'])
        except Error:
            cls.engine.stop()
            if not cls.headless and not os.environ.get('DISPLAY'):
                raise unittest.SkipTest('真实 headed Chromium 启动失败：没有 DISPLAY/X server；未安装或伪造 Xvfb')
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.engine.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        # Allow headed CI rendering to settle (5s raced a loaded headed runner);
        # negative cases set their own short bound.
        self.page.set_default_timeout(15000)
        self.requests = []

    def load(self, html):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        self.page.goto(HOST + '/login', wait_until='load')
        self.assertEqual(self.page.evaluate('document.readyState'), 'complete')

    def test_ready_login_submits_without_logging_credentials(self):
        self.load(FORM)
        logs = []
        self.assertTrue(submit_recognized_login(self.page, 'fixture-user', 'fixture-password', log=logs.append))
        self.assertTrue(self.page.evaluate('window.submitted === true'))
        self.assertNotIn('fixture-password', '\n'.join(logs))
        self.assertNotIn('fixture-user', '\n'.join(logs))

    def test_disabled_form_controls_wait_for_enable_after_load(self):
        html = FORM.replace('<input ', '<input disabled ').replace('<button ', '<button disabled ')
        self.load(html)
        self.page.evaluate("setTimeout(()=>document.querySelectorAll('input,button').forEach(n=>n.disabled=false),200)")
        self.assertTrue(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        self.assertTrue(self.page.evaluate('window.submitted === true'))

    def test_permanently_disabled_controls_timeout_without_submission(self):
        self.load(FORM.replace('<input ', '<input disabled '))
        self.page.set_default_timeout(250)
        self.assertFalse(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        self.assertFalse(self.page.evaluate('window.submitted === true'))

    def test_form_inserted_after_load_is_awaited_before_recognition(self):
        self.load('<main>Loading authentication form</main>')
        self.page.evaluate('(html)=>setTimeout(()=>document.body.innerHTML=html,200)', FORM)
        self.assertTrue(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        self.assertTrue(self.page.evaluate('window.submitted === true'))

    def test_enabled_submit_is_attempt_only_until_caller_verifies_navigation(self):
        self.load(FORM.replace('window.submitted=true', 'window.earlySubmit=true'))
        # Leave the authentication handler uninitialized for this attempt rather
        # than racing a wall-clock timer against headed CI actionability checks.
        self.assertTrue(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        # True is explicitly an attempted click, never authenticated success.
        self.assertTrue(self.page.evaluate('window.earlySubmit === true'))
        self.assertFalse(self.page.evaluate('window.submitted === true'))
        self.assertEqual(self.page.url, HOST + '/login')

    def test_cross_origin_formaction_blocks_credentials_before_fill(self):
        self.load(FORM.replace('<button type="submit"', '<button formaction="https://unapproved.invalid/collect" type="submit"'))
        self.assertFalse(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        self.assertEqual(self.page.locator('input[name=j_username]').input_value(), '')
        self.assertEqual(self.page.locator('input[type=password]').input_value(), '')

    def test_form_target_changed_while_disabled_is_rechecked_after_enable(self):
        self.load(FORM.replace('<input ', '<input disabled '))
        self.page.evaluate("""setTimeout(()=>{
          document.querySelector('form').action='https://unapproved.invalid/collect';
          document.querySelectorAll('input').forEach(n=>n.disabled=false);
        },200)""")
        self.assertFalse(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        self.assertEqual(self.page.locator('input[name=j_username]').input_value(), '')
        self.assertEqual(self.page.locator('input[type=password]').input_value(), '')

    def test_cross_origin_form_action_blocks_credentials(self):
        self.load(FORM.replace('<form ', '<form action="https://unapproved.invalid/collect" '))
        self.assertFalse(submit_recognized_login(self.page, 'fixture-user', 'fixture-password'))
        self.assertEqual(self.page.locator('input[type=password]').input_value(), '')

    def test_native_form_invalidity_prevents_submit(self):
        self.load(FORM.replace('name="j_username"', 'name="j_username" type="email" required'))
        self.assertFalse(submit_recognized_login(self.page, 'invalid-email', 'fixture-password'))
        self.assertFalse(self.page.evaluate('window.submitted === true'))

    def _two_step(self, allow_other=False):
        other = 'https://unapproved.invalid/second'
        first = '<form onsubmit="event.preventDefault();location.href='+repr(other)+'"><input name="loginfmt"><button type="submit">Next</button></form>'
        second = '<form onsubmit="event.preventDefault();window.submitted=true"><input type="password"><button type="submit">Sign in</button></form>'
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=second if route.request.url == other else first))
        self.page.goto(HOST + '/login')
        callback = (lambda url:urlsplit(url).hostname in ['jenkins.auth-fixture.invalid','unapproved.invalid']) if allow_other else None
        result = submit_recognized_login(self.page, 'fixture-user', 'fixture-password', validate_target=callback)
        self.assertEqual(self.page.url, other)
        return result

    def test_two_step_redirect_revalidates_before_password(self):
        self.assertFalse(self._two_step())
        self.assertEqual(self.page.locator('input[type=password]').input_value(), '')
        self.assertFalse(self.page.evaluate('window.submitted === true'))

    def test_two_step_explicit_host_callback_allows_approved_sso(self):
        self.assertTrue(self._two_step(allow_other=True))
        self.assertTrue(self.page.evaluate('window.submitted === true'))

    def test_expired_session_recovers_exact_build_with_disabled_login_fixture(self):
        auth = Path(self.temp.name) / 'plugin.json'
        self.context.add_cookies([{'name':'session','value':'expired','url':HOST}])
        self.context.storage_state(path=str(auth))
        login = FORM.replace('onsubmit="event.preventDefault(); window.submitted=true"',
                             'action="/j_security_check" method="post"')
        login = login.replace('<input ', '<input disabled ').replace('<button ', '<button disabled ')
        login += '<script>window.addEventListener("load",()=>setTimeout(()=>document.querySelectorAll("input,button").forEach(n=>n.disabled=false),200))</script>'
        def route(request):
            path = urlsplit(request.request.url).path
            self.requests.append((request.request.method,path))
            if path == '/login':
                request.fulfill(content_type='text/html', body=login)
            elif path == '/j_security_check':
                request.fulfill(content_type='text/html', headers={'set-cookie':'session=valid; Secure; Path=/'},
                                body='<script>location.replace('+json.dumps(TARGET)+')</script>')
            elif request.request.url == TARGET:
                if 'session=valid' in request.request.headers.get('cookie',''):
                    request.fulfill(content_type='text/html', body='<title>Build 42</title><h1>Build 42</h1>')
                else:
                    request.fulfill(status=403, content_type='text/html', body='<h1>Forbidden</h1><a href="/login">Sign in</a>')
            else:
                request.fulfill(status=404, body='Unexpected offline fixture route')
        self.context.route('**/*', route)
        def fresh(**kwargs):
            context = self.browser.new_context(**kwargs)
            context.route('**/*', route)
            self.addCleanup(context.close)
            return context
        worker = object.__new__(JenkinsBrowser)
        worker.browser = SimpleNamespace(new_context=fresh)
        worker.context, worker.page = self.context, self.page
        worker.site, worker.credentials = 'plugin', ('fixture-user', 'fixture-password')
        worker.visible, worker.manual_login = not self.headless, False
        worker.conf, worker.auth_path = {'auth_allowed_hosts':[]}, auth
        self.assertEqual(worker.visit(TARGET, timeout=3000), 200)
        self.assertEqual(worker.page.url, TARGET)
        self.assertEqual(self.requests.count(('POST','/j_security_check')), 1)
        self.assertFalse(any('/build' in path for _,path in self.requests))
        self.assertEqual(json.loads(auth.read_text())['cookies'][0]['value'], 'valid')
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)


class HeadlessAuthReadinessTests(AuthReadinessFixtures, unittest.TestCase):
    headless = True


class HeadedAuthReadinessTests(AuthReadinessFixtures, unittest.TestCase):
    headless = False
