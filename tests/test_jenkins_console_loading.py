"""Offline reproduction: Callback #2 completed, but consoleText loads late."""
import unittest
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from appbuilder.jenkins import JenkinsBrowser, result_from_status_markup


class Loc:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind

    def count(self):
        if self.kind == 'body':
            return 1
        return len(self.page.icons) if self.page.active == 'build' else 0

    def nth(self, n):
        obj = Loc(self.page, self.kind)
        obj.n = n
        return obj

    def evaluate(self, expr):
        if self.kind == 'body':
            return self.page.console
        return self.page.icons[getattr(self, 'n', 0)]


class DeferredConsolePage:
    def __init__(self, console='[Pipeline] cleanup in progress', final=None, icons=()):
        self.active = 'console'
        self.console = console
        self.final = final
        self.icons = list(icons)
        self.waits = []

    def locator(self, css):
        return Loc(self, 'body' if css == 'body' else 'icons')

    def wait_for_function(self, script, *, timeout, polling):
        self.waits.append((script, timeout, polling))
        if self.final is None:
            raise PlaywrightTimeout('console still loading')
        self.console += '\n' + self.final
        return True


class Browser:
    site = 'plugin'
    poll = 0.01
    conf = {'build_timeout_minutes': 1, 'console_settle_seconds': 10}

    def __init__(self, page):
        self.page = page
        self.visited = []

    def visit(self, url, timeout=60000):
        self.visited.append(url)
        self.page.active = 'console' if url.endswith('/consoleText') else 'build'
        return 200

    def find_callback(self, *_):
        raise AssertionError('Must not follow another callback when #2 is terminal')


class ConsoleLoadingTests(unittest.TestCase):
    def test_late_finished_marker_read_before_navigating_away(self):
        page = DeferredConsolePage(final='Finished: SUCCESS')
        browser = Browser(page)
        result, log = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)
        self.assertEqual(result, 'SUCCESS')
        self.assertTrue(log.endswith('Finished: SUCCESS'))
        self.assertEqual(browser.visited, ['https://ci.corp/job/p/2/consoleText'])
        self.assertEqual(page.waits[0][1], 10000)
        self.assertIn(r'\r?\n', page.waits[0][0])
        self.assertIn(r'\s*', page.waits[0][0])

    def test_fallback_to_build_heading_svg_status_when_console_incomplete(self):
        markup = '<svg class="svg-icon icon-md"><use href="/images/svgs/status/blue.svg#blue"></use></svg>'
        page = DeferredConsolePage(icons=[markup])
        browser = Browser(page)
        result, log = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)
        self.assertEqual((result, log), ('SUCCESS', ''))
        self.assertEqual(browser.visited, [
            'https://ci.corp/job/p/2/consoleText', 'https://ci.corp/job/p/2/'])

    def test_failure_heading_takes_precedence_when_console_unavailable(self):
        markup = '<svg><use href="/images/svgs/status/red.svg#red" /></svg>'
        result, _ = JenkinsBrowser.wait_result(
            Browser(DeferredConsolePage(icons=[markup])), 'https://ci.corp/job/p/', 2)
        self.assertEqual(result, 'FAILURE')

    def test_running_icon_must_not_be_inferred_successful_from_title(self):
        self.assertIsNone(result_from_status_markup('<svg class="icon-running" title="Success" />'))

    def test_no_false_positive_for_running_build_with_old_success_log(self):
        markup = '<svg class="icon-running"><use href="/images/svgs/status/blue.svg#blue"></use></svg>'
        self.assertIsNone(result_from_status_markup(markup))

    def test_svg_icons_without_status_are_not_success(self):
        self.assertIsNone(result_from_status_markup('<svg class="svg-icon icon-md"><use href="/images/logo.svg#logo"/></svg>'))


if __name__ == '__main__':
    unittest.main()
