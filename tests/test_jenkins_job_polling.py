"""Job-discovery polling tests: no Jenkins/network access."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from appbuilder.jenkins import JenkinsBrowser, NeedsAttention


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TestJobDiscoveryPolling(unittest.TestCase):
    @staticmethod
    def browser(responses, *, timeout_minutes=1, interval=10):
        obj = object.__new__(JenkinsBrowser)
        obj.site = 'plugin'
        obj.conf = {'discover_timeout_minutes': timeout_minutes, 'poll_seconds': 45}
        obj.poll = 45
        obj.discover_poll = interval
        obj.visit = Mock(side_effect=responses)
        obj.page = Mock()
        obj.page.title.return_value = 'Release branch job - Jenkins'
        return obj

    def test_404_then_created_checks_every_ten_seconds(self):
        b = self.browser([404, 404, 200])
        clock = Clock()
        with patch('appbuilder.jenkins.time.monotonic', clock.monotonic), \
             patch('appbuilder.jenkins.time.sleep', clock.sleep), \
             patch('appbuilder.jenkins.action') as trace, \
             contextlib.redirect_stdout(io.StringIO()):
            b.wait_job('https://ci.company/job/release_1_3/')
        self.assertEqual(b.visit.call_count, 3)
        self.assertEqual(clock.sleeps, [10, 10])
        self.assertTrue(any('10s 后重试' in str(call) for call in trace.call_args_list))
        self.assertTrue(all('45s 后重试' not in str(call) for call in trace.call_args_list))

    def test_403_stops_without_retry(self):
        b = self.browser([403])
        clock = Clock()
        with patch('appbuilder.jenkins.time.monotonic', clock.monotonic), \
             patch('appbuilder.jenkins.time.sleep', clock.sleep), \
             patch('appbuilder.jenkins.action'):
            with self.assertRaisesRegex(NeedsAttention, 'HTTP 403'):
                b.wait_job('https://ci.company/job/release_1_3/')
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(b.visit.call_count, 1)

    def test_persistent_404_times_out_without_click(self):
        b = self.browser([404] * 10, timeout_minutes=0.35)
        clock = Clock()
        with patch('appbuilder.jenkins.time.monotonic', clock.monotonic), \
             patch('appbuilder.jenkins.time.sleep', clock.sleep), \
             patch('appbuilder.jenkins.action'):
            with self.assertRaisesRegex(TimeoutError, '最后 HTTP=404') as exc:
                b.wait_job('https://ci.company/job/release_1_3/')
        self.assertIn('分支', str(exc.exception))
        self.assertEqual(b.visit.call_count, 3)
        self.assertEqual(clock.sleeps, [10, 10, 1.0])


if __name__ == '__main__':
    unittest.main()
