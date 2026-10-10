"""Real Chromium DOM tests: exact Jenkins build row's green status icon is recognized."""
import unittest
from types import SimpleNamespace
from playwright.sync_api import sync_playwright
from appbuilder.jenkins import JenkinsBrowser, result_from_status_markup


PAGE_TEMPLATE = '''
<html><body>
<section id="buildHistory">
  <div class="build-row">
    <svg class="jenkins-icon"><use href="/images/svgs/status/blue.svg#blue"></use></svg>
    <a href="https://ci.corp/job/p/1/">#1</a>
  </div>
  <div class="build-row" id="target-row">
    <svg class="jenkins-icon"><use href="{current_icon}"></use></svg>
    <a href="https://ci.corp/job/p/2/">#2</a>
  </div>
</section>
</body></html>
'''


class JenkinsGreenTickDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.fake = SimpleNamespace(site='plugin', page=self.page)

    def tearDown(self):
        self.page.close()

    def test_only_exact_build_2_green_tick_counts(self):
        self.page.set_content(PAGE_TEMPLATE.format(current_icon='/images/svgs/status/blue.svg#blue'))
        self.assertEqual(JenkinsBrowser._history_result(self.fake, 'https://ci.corp/job/p/', 2), 'SUCCESS')

    def test_previous_build_1_green_does_not_mark_running_2_successful(self):
        self.page.set_content(PAGE_TEMPLATE.format(current_icon='/images/svgs/status/blue_anime.svg#blue-anime'))
        self.assertIsNone(JenkinsBrowser._history_result(self.fake, 'https://ci.corp/job/p/', 2))

    def test_failure_of_exact_build(self):
        self.page.set_content(PAGE_TEMPLATE.format(current_icon='/images/svgs/status/red.svg#red'))
        self.assertEqual(JenkinsBrowser._history_result(self.fake, 'https://ci.corp/job/p/', 2), 'FAILURE')

    def test_build_link_from_another_job_cannot_supply_status(self):
        self.page.set_content(PAGE_TEMPLATE.format(current_icon='/images/svgs/status/blue.svg#blue').replace(
            'https://ci.corp/job/p/2/', 'https://ci.corp/job/another/2/'))
        self.assertIsNone(JenkinsBrowser._history_result(self.fake, 'https://ci.corp/job/p/', 2))

    def test_no_outcome_from_plain_check_without_status_semantics(self):
        self.assertIsNone(result_from_status_markup('<svg class="icon-check"/>'))

    def test_recognizes_newer_status_success_class(self):
        self.assertEqual(result_from_status_markup('<svg class="jenkins-build-status-success"></svg>'), 'SUCCESS')

    def test_visible_tick_stops_without_downloading_console(self):
        self.page.set_content(PAGE_TEMPLATE.format(current_icon='/images/svgs/status/blue.svg#blue'))
        class B:
            site='plugin'; result_poll=5; poll=45; conf={'build_timeout_minutes': 2}
        worker = B()
        worker.page=self.page
        worker.context=SimpleNamespace(request=SimpleNamespace(
            get=lambda *args, **kwargs: self.fail('No consoleText should be downloaded')))
        worker.visit=lambda url:200
        worker.find_callback=lambda *args:None
        self.assertEqual(JenkinsBrowser.wait_result(worker, 'https://ci.corp/job/p/', 2), ('SUCCESS', ''))


if __name__ == '__main__': unittest.main()
