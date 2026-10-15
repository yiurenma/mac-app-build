"""APP link via Jenkins API token: same evidence chain as the browser path, no browser."""
import unittest

from appbuilder.app_links import token_lookup
from appbuilder.jenkins import NeedsAttention
from appbuilder.jenkins_api_links import api_lookup
from test_jenkins_links import GMX_JOB, JOIN, SOURCE, UPLOAD, block, console_html


class FakeJenkins:
    def __init__(self, pages=None, status=200, redirect=False):
        self.pages = pages if pages is not None else {
            SOURCE['build_url'] + 'consoleFull': console_html('build start\n' + block()),
            UPLOAD['build_url'] + 'consoleFull': console_html('uploading\nShare: ' + JOIN + '\n'),
        }
        self.status, self.redirect, self.urls = status, redirect, []

    def __call__(self, url):
        self.urls.append(url)
        if self.redirect:
            return 302, url, ''
        if self.status != 200:
            return self.status, url, ''
        if url not in self.pages:
            return 404, url, ''
        return 200, url, self.pages[url]


class ApiLookupTests(unittest.TestCase):
    def test_reads_source_then_upload_console_and_returns_the_join_link(self):
        jenkins = FakeJenkins()
        result = api_lookup(GMX_JOB, 812, jenkins)
        self.assertEqual(result['share_url'], JOIN)
        self.assertEqual(result['downstream_build_url'], UPLOAD['build_url'])
        self.assertEqual(jenkins.urls, [SOURCE['build_url'] + 'consoleFull', UPLOAD['build_url'] + 'consoleFull'])

    def test_refused_token_or_sso_redirect_is_reported_not_followed(self):
        for jenkins, words in ((FakeJenkins(status=401), 'HTTP 401'), (FakeJenkins(status=403), 'HTTP 403'),
                               (FakeJenkins(redirect=True), '重定向')):
            with self.subTest(words=words), self.assertRaisesRegex(NeedsAttention, words):
                api_lookup(GMX_JOB, 812, jenkins)

    def test_failed_upload_segment_is_not_accepted(self):
        pages = {SOURCE['build_url'] + 'consoleFull': console_html(block(result='FAILURE'))}
        with self.assertRaises(NeedsAttention):
            api_lookup(GMX_JOB, 812, FakeJenkins(pages))


class TokenLookupTests(unittest.TestCase):
    def test_success_marks_transport_and_failure_returns_none_for_browser_fallback(self):
        messages = []
        ok = token_lookup({}, SOURCE, emit=messages.append, fetch=FakeJenkins())
        self.assertEqual((ok['share_url'], ok['transport']), (JOIN, 'jenkins-api-token'))
        self.assertIsNone(token_lookup({}, SOURCE, emit=messages.append, fetch=FakeJenkins(status=403)))
        self.assertTrue(any('改用浏览器' in m and 'HTTP 403' in m for m in messages))

    def test_no_saved_token_means_browser_without_any_request(self):
        from unittest.mock import patch
        with patch('appbuilder.auth.jenkins_api_credentials', return_value=None):
            self.assertIsNone(token_lookup({}, SOURCE, emit=lambda m: None))


if __name__ == '__main__':
    unittest.main()
