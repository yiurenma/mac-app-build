"""Regression: completed Jenkins #2 consoleText is available before Chrome DOM finishes."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from appbuilder.jenkins import JenkinsBrowser, NeedsAttention


class RawReply:
    def __init__(self, payload, status=200, url='https://ci.corp/job/p/2/consoleText'):
        self.payload = payload
        self.status = status
        self.url = url
    def text(self):
        return self.payload


class RawRequests:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
    def get(self, url, **opts):
        self.calls.append((url, opts))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class BuildStatusIcon:
    def __init__(self, heading):
        self.heading = heading
    def count(self):
        return int(self.heading is not None)
    def nth(self, index):
        return self
    def evaluate(self, expression):
        return f'<svg class="icon-{self.heading.lower()}" title="{self.heading.title()}"/>'


class RawPage:
    def __init__(self, owner):
        self.owner = owner
    def locator(self, selector):
        self.owner.status_checks += 1
        return BuildStatusIcon(self.owner.heading)


class RawBrowser:
    site = 'plugin'
    poll = 45
    result_poll = 5
    conf = {'build_timeout_minutes': 2}
    def __init__(self, replies, page_code=200, heading=None, callback=None):
        self.context = SimpleNamespace(request=RawRequests(replies))
        self.page_code = page_code
        self.heading = heading
        self.callback = callback
        self.page = RawPage(self)
        self.visits = []
        self.status_checks = 0
    def visit(self, url, **kwargs):
        self.visits.append(url)
        return self.page_code
    def find_callback(self, *args):
        return self.callback


class FastResultTests(unittest.TestCase):
    def test_callback_success_identified_from_raw_http_no_dom(self):
        browser = RawBrowser([RawReply('[Pipeline] done\nFinished: SUCCESS\n')])
        outcome, tail = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)
        self.assertEqual(outcome, 'SUCCESS')
        self.assertIn('Finished: SUCCESS', tail)
        self.assertEqual(browser.visits, ['https://ci.corp/job/p/', 'https://ci.corp/job/p/2/'])
        self.assertEqual(browser.context.request.calls[0][1]['timeout'], 10000)

    def test_gmx_failure_uses_same_fast_detector(self):
        browser = RawBrowser([RawReply('Finished: FAILURE\n')])
        browser.site = 'gmx'
        self.assertEqual(JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)[0], 'FAILURE')
        self.assertEqual(browser.visits, ['https://ci.corp/job/p/', 'https://ci.corp/job/p/2/'])

    def test_running_console_then_finishes_with_five_second_poll(self):
        browser = RawBrowser([RawReply('stage work\n'), RawReply('Finished: SUCCESS\n')])
        with patch('appbuilder.jenkins.time.sleep') as sleep:
            result = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)
        self.assertEqual(result[0], 'SUCCESS')
        self.assertEqual(len(browser.context.request.calls), 2)
        sleep.assert_called_once()
        self.assertLessEqual(sleep.call_args.args[0], 5)
        self.assertGreater(sleep.call_args.args[0], 0)

    def test_previous_success_in_nonterminal_console_is_not_accepted(self):
        browser = RawBrowser([RawReply('other step Finished: SUCCESS\nactually running\n'),
                              RawReply('running\nFinished: FAILURE')])
        with patch('appbuilder.jenkins.time.sleep'):
            result = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)
        self.assertEqual(result[0], 'FAILURE')

    def test_header_fallback_when_raw_console_is_incomplete(self):
        browser = RawBrowser([RawReply('not yet rendered')], heading='SUCCESS')
        result, tail = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)
        self.assertEqual((result, tail), ('SUCCESS', ''))
        self.assertEqual(browser.visits, ['https://ci.corp/job/p/', 'https://ci.corp/job/p/2/'])

    def test_trigger_callback_fallback_still_works(self):
        browser = RawBrowser([RawReply('still running')], callback=3)
        result = JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2, watch_callback=True)
        self.assertEqual(result, ('DELEGATED', '3'))

    def test_request_timeout_does_not_claim_success(self):
        browser = RawBrowser([PlaywrightTimeout('request timeout')], heading='FAILURE')
        self.assertEqual(JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)[0], 'FAILURE')

    def test_raw_console_403_stops_after_one_read_only_retry(self):
        browser = RawBrowser([RawReply('', 403), RawReply('', 403)])
        with self.assertRaisesRegex(NeedsAttention, 'HTTP 403'):
            JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)

    def test_sso_redirect_not_confused_with_console(self):
        browser = RawBrowser([RawReply('Finished: SUCCESS', url='https://login.company.example/sso')], heading='FAILURE')
        self.assertEqual(JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)[0], 'FAILURE')

    def test_nonterminal_http_404_can_fallback_to_build_page_status(self):
        browser = RawBrowser([RawReply('', 404)], heading='SUCCESS')
        self.assertEqual(JenkinsBrowser.wait_result(browser, 'https://ci.corp/job/p/', 2)[0], 'SUCCESS')


if __name__ == '__main__': unittest.main()
