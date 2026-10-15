"""APP link via the Jenkins API token: plain-text logs plus Jenkins' upstream record."""
import json
import unittest
from unittest.mock import patch

from appbuilder.app_links import token_lookup
from appbuilder.jenkins import NeedsAttention
from appbuilder.jenkins_api import JenkinsHttp
from appbuilder.jenkins_api_links import api_lookup, upload_build_from_text

HOST = 'https://jenkins.link-fixture.invalid'
GMX_JOB = HOST + '/job/GMX/job/upload%2FAUTO-1/'
SOURCE = {'job_url': GMX_JOB, 'number': 2, 'build_url': GMX_JOB + '2/'}
UPLOAD_JOB = HOST + '/job/ci/job/shared/job/upload-to-testfairy/job/master/'
JOIN = 'https://tenant.testfairy.com/join/svpzsf?id=575185'


def gmx_log(result='SUCCESS', number=38220, extra=''):
    return ('Started by user George\n[Pipeline] stage\n'
            '[Pipeline] build (Building ci » shared » upload-to-testfairy » master)\n'
            'Scheduling project: ci » shared » upload-to-testfairy » master\n'
            f'Starting building: ci » shared » upload-to-testfairy » master #{number}\n'
            f'Build ci » shared » upload-to-testfairy » master #{number} completed: {result}\n'
            '[Pipeline] }\n' + extra + 'Finished: SUCCESS\n')


class FakeHttp(JenkinsHttp):
    def __init__(self, gmx=None, upload_log=None, upload=None, full_name='GMX/upload%2FAUTO-1', status=200):
        self.calls = []
        self.status = status
        self.texts = {GMX_JOB + '2/consoleText': gmx if gmx is not None else gmx_log(),
                      # Plain text shows only the label; the link itself is a hyperlink in the page.
                      UPLOAD_JOB + '38220/consoleText': 'Uploading...\nShare this build: Open in TestFairy\n',
                      UPLOAD_JOB + '38220/consoleFull': upload_log if upload_log is not None else (
                          '<html><body><pre class="console-output">Uploading...\nShare this build: '
                          f'<a href="{JOIN.replace("&", "&amp;")}">Open in TestFairy</a>\nFinished: SUCCESS\n</pre></body></html>')}
        self.jsons = {GMX_JOB + 'api/json': {'fullName': full_name},
                      UPLOAD_JOB + '38220/api/json': upload if upload is not None else {
                          'building': False, 'result': 'SUCCESS',
                          'actions': [{'causes': [{'upstreamProject': 'GMX/upload%2FAUTO-1', 'upstreamBuild': 2}]}]}}

    def request(self, method, url, data=None, accept='application/json'):
        self.calls.append(url)
        if self.status != 200:
            return self.status, url, {}, b''
        path = url.split('?')[0]
        if path in self.texts:
            return 200, url, {'Content-Type': 'text/plain'}, self.texts[path].encode()
        if path in self.jsons:
            return 200, url, {'Content-Type': 'application/json'}, json.dumps(self.jsons[path]).encode()
        return 404, url, {}, b''


class TextParsingTests(unittest.TestCase):
    def test_first_upload_segment_and_job_url_from_plain_text(self):
        self.assertEqual(upload_build_from_text(gmx_log(), HOST + '/'), (UPLOAD_JOB, 38220))

    def test_failed_or_incomplete_first_segment_is_refused(self):
        for text in (gmx_log(result='FAILURE'), gmx_log().replace('completed: SUCCESS', ''),
                     'no upload here\nFinished: SUCCESS\n'):
            with self.subTest(text=text[:40]), self.assertRaises(NeedsAttention):
                upload_build_from_text(text, HOST + '/')


class ApiLookupTests(unittest.TestCase):
    def test_reads_logs_checks_upstream_record_and_returns_the_link(self):
        http = FakeHttp()
        result = api_lookup(GMX_JOB, 2, http)
        self.assertEqual(result['share_url'], JOIN)
        self.assertEqual(result['downstream_build_url'], UPLOAD_JOB + '38220/')
        # The big GMX log is read as plain text; only the small upload page is HTML.
        self.assertNotIn(GMX_JOB + '2/consoleFull', http.calls)
        self.assertIn(UPLOAD_JOB + '38220/consoleFull', http.calls)

    def test_upload_build_triggered_by_another_build_is_refused(self):
        other = {'building': False, 'result': 'SUCCESS',
                 'actions': [{'causes': [{'upstreamProject': 'GMX/upload%2FAUTO-1', 'upstreamBuild': 1}]}]}
        with self.assertRaisesRegex(NeedsAttention, '上游'):
            api_lookup(GMX_JOB, 2, FakeHttp(upload=other))

    def test_link_comes_from_the_upload_page_hyperlink_and_must_be_unique(self):
        two = ('<html><body><pre class="console-output">'
               f'<a href="{JOIN}">a</a> <a href="https://tenant.testfairy.com/join/other">b</a></pre></body></html>')
        with self.assertRaises(NeedsAttention):
            api_lookup(GMX_JOB, 2, FakeHttp(upload_log=two))
        none = '<html><body><pre class="console-output">no link here</pre></body></html>'
        with self.assertRaises(NeedsAttention):
            api_lookup(GMX_JOB, 2, FakeHttp(upload_log=none))

    def test_unfinished_upload_build_is_refused(self):
        running = {'building': True, 'result': None, 'actions': []}
        with self.assertRaises(NeedsAttention):
            api_lookup(GMX_JOB, 2, FakeHttp(upload=running))


class TokenLookupTests(unittest.TestCase):
    def test_success_marks_transport_and_failure_returns_none_for_browser_fallback(self):
        messages = []
        ok = token_lookup({}, SOURCE, emit=messages.append, http=FakeHttp())
        self.assertEqual((ok['share_url'], ok['transport']), (JOIN, 'jenkins-api-token'))
        self.assertIsNone(token_lookup({}, SOURCE, emit=messages.append, http=FakeHttp(status=403)))
        self.assertTrue(any('改用浏览器' in m and 'HTTP 403' in m for m in messages))

    def test_no_saved_token_means_browser_without_any_request(self):
        with patch('appbuilder.auth.jenkins_api_credentials', return_value=None):
            self.assertIsNone(token_lookup({}, SOURCE, emit=lambda m: None))


if __name__ == '__main__':
    unittest.main()
