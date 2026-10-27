"""Real browser regression for Jenkins labels covering Boolean parameter inputs."""
import unittest
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from appbuilder.jenkins import JenkinsBrowser, NeedsAttention
from chromium_path import CHROMIUM


class CheckboxLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = sync_playwright().start()
        try:
            cls.browser = cls.engine.chromium.launch(
                executable_path=CHROMIUM, headless=True, args=['--no-sandbox'])
        except Exception:
            cls.engine.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.engine.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_covering_attached_label_checks_and_unchecks_exact_input(self):
        self.page.set_content('''<style>
          .row { position:relative; height:40px; }
          label { position:absolute; inset:0; background:white; }
        </style><div class="row"><input id="sync" name="value" type="checkbox">
        <label class="attach-previous" for="sync">SYNC_UPLOAD</label></div>
        <input id="other" type="checkbox" checked>
        <script>window.events=[]; document.querySelector('#sync').addEventListener(
          'change', e=>events.push(e.target.checked));</script>''')
        control = self.page.locator('#sync')
        with self.assertRaises(PlaywrightTimeout):
            control.check(timeout=100)
        JenkinsBrowser._set_checkbox(control, True, 'SYNC_UPLOAD')
        self.assertTrue(control.is_checked())
        JenkinsBrowser._set_checkbox(control, False, 'SYNC_UPLOAD')
        self.assertFalse(control.is_checked())
        self.assertTrue(self.page.locator('#other').is_checked())
        self.assertEqual(self.page.evaluate('events'), [True, False])

    def test_hidden_input_uses_unique_standard_associated_label(self):
        self.page.set_content('<input id="force" type="checkbox" style="display:none">'
                              '<label for="force">FORCE</label>')
        control = self.page.locator('#force')
        JenkinsBrowser._set_checkbox(control, True, 'FORCE')
        self.assertTrue(control.is_checked())

    def test_unrelated_overlay_is_not_clicked(self):
        self.page.set_content('''<input id="sync" type="checkbox">
            <div style="position:fixed;inset:0;background:white" onclick="window.clicked=true">overlay</div>''')
        control = self.page.locator('#sync')
        with self.assertRaisesRegex(NeedsAttention, '关联标签'):
            JenkinsBrowser._set_checkbox(control, True, 'SYNC_UPLOAD')
        self.assertFalse(control.is_checked())
        self.assertFalse(self.page.evaluate('Boolean(window.clicked)'))

    def test_disabled_checkbox_is_never_changed(self):
        self.page.set_content('<input id="sync" type="checkbox" disabled>'
                              '<label for="sync">SYNC_UPLOAD</label>')
        control = self.page.locator('#sync')
        with self.assertRaisesRegex(NeedsAttention, '禁用'):
            JenkinsBrowser._set_checkbox(control, True, 'SYNC_UPLOAD')
        self.assertFalse(control.is_checked())

    def test_already_selected_does_not_toggle(self):
        self.page.set_content('<input id="sync" type="checkbox" checked>'
                              '<label for="sync" onclick="window.clicked=true">SYNC_UPLOAD</label>')
        control = self.page.locator('#sync')
        JenkinsBrowser._set_checkbox(control, True, 'SYNC_UPLOAD')
        self.assertTrue(control.is_checked())
        self.assertFalse(self.page.evaluate('Boolean(window.clicked)'))
