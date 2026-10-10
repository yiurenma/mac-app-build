"""Exercise production download/diagnosis using a simulated Jenkins response.

JSON shape/Journey digest come from the screenshot, not a full raw capture.
DomainModel digest and surrounding log lines are synthetic.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder import diagnose
from appbuilder.config import load_config
from appbuilder.jenkins import plugin_upload_hashes
from test_nexus_field_format import payload, FULL, HASHES, JOURNEY


class SimulatedJenkinsTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.job = self.config['jenkins']['plugin_parent'] + 'release_1_3_202610101332/'
        self.calls = []
        self.expected = {'version': FULL, 'journey_sha256': JOURNEY,
                         'domainmodel_sha256': HASHES['PayByBankDomainModelPluginKit']}

    def browser_factory(self, text):
        calls, job = self.calls, self.job
        class Browser:
            def __init__(self, *args, **kwargs):
                self.page = SimpleNamespace(url=job)
                self.context = SimpleNamespace(request=SimpleNamespace(get=self.get))
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def visit(self, url, **kwargs):
                calls.append(('visit', url))
                self.page.url = url
                return 200
            def build_numbers(self, job): return {1, 2}
            def _history_result(self, job, number): return 'SUCCESS'
            def _heading_result(self, number): return 'SUCCESS'
            def get(self, url, **kwargs):
                calls.append(('get', url))
                return SimpleNamespace(status=200, url=url,
                    headers={'content-type': 'text/plain;charset=utf-8'}, text=lambda: text)
            def request_build(self, *args, **kwargs):
                raise AssertionError('Read-only diagnosis must never trigger a build')
        return Browser

    def run_diagnosis(self, text, expected=None):
        with tempfile.TemporaryDirectory() as td, patch.object(diagnose, 'DATA', Path(td)):
            report = diagnose.run_diagnosis(self.job + '2/', config=self.config,
                factory=self.browser_factory(text), expected_artifacts=expected or self.expected,
                emit=lambda _: None)
            saved = json.loads((Path(td) / 'diagnostics/plugin/last-result.json').read_text())
        self.assertIn('comparisons', report, report)
        self.assertEqual(saved['comparisons'], report['comparisons'])
        self.assertEqual(self.calls, [('visit', self.job), ('visit', self.job + '2/'),
                                     ('get', self.job + '2/consoleText')])
        return report

    def test_large_log_download_to_comparison_and_saved_result(self):
        # >10 MB; the desired JSON is in the middle, outside a tail-only window.
        noise = '[2026-10-10T06:00:00.000Z] unrelated build output ' + 'x' * 60 + '\n'
        text = noise * 55000 + payload('[2026-10-10T06:33:40.123Z] ').replace('Finished: SUCCESS', '') + '\n' + noise * 55000
        self.assertGreater(len(text), 10_000_000)
        report = self.run_diagnosis(text)
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['artifact_result']['status'], 'PASS')
        self.assertEqual({r['field']: r['actual'] for r in report['comparisons']}, self.expected)
        self.assertTrue(all(r['match'] for r in report['comparisons']))
        self.assertIn('结束标记=NOT FOUND', '\n'.join(report['logs']))

    def test_actual_sha_is_not_copied_from_expected_input(self):
        expected = {**self.expected, 'journey_sha256': 'f' * 64}
        report = self.run_diagnosis(payload(), expected)
        self.assertEqual(report['verdict'], 'FAIL')
        checks = {r['field']: r for r in report['comparisons']}
        self.assertEqual(checks['journey_sha256']['actual'], JOURNEY)
        self.assertEqual([r['field'] for r in report['comparisons'] if not r['match']], ['journey_sha256'])

    def test_other_tarball_version_cannot_pass(self):
        report = self.run_diagnosis(payload().replace(FULL, FULL[:-1] + '3'))
        self.assertEqual(report['verdict'], 'FAIL')
        version = next(r for r in report['comparisons'] if r['field'] == 'version')
        self.assertEqual(version['actual'], FULL[:-1] + '3')
        self.assertFalse(version['match'])

    def test_missing_group_fails_even_with_success_icon(self):
        report = self.run_diagnosis('Finished: SUCCESS\n')
        self.assertEqual(report['status_verdict'], 'PASS')
        self.assertEqual(report['verdict'], 'FAIL')
        self.assertTrue(all(r['actual'] is None for r in report['comparisons']))

    def test_formal_build_uses_same_field_format(self):
        factory = self.browser_factory(payload('[2026-10-10T06:33:40.123Z] '))
        self.assertEqual(plugin_upload_hashes(factory(), self.job, 2, FULL), HASHES)
        self.assertEqual(self.calls, [('get', self.job + '2/consoleText')])

if __name__ == '__main__': unittest.main()
