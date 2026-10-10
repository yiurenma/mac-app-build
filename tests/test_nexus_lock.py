import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from appbuilder.nexus import parse_upload_hashes
from appbuilder.edits import update_lock_from_hashes, validate_lock
from appbuilder.jenkins import plugin_upload_hashes, NeedsAttention
from appbuilder.versions import ARTIFACTS
from appbuilder.verify import gmx_commit

VERSION = '1.3.1-alpha.1-SNAPSHOT.2'
HASHES = {name: str(i + 1) * 64 for i, name in enumerate(ARTIFACTS)}


def rows():
    return [{'artifactUrl': f'https://nexus.invalid/plugin/{VERSION}/{name}-{VERSION}.tar.gz',
             'version': VERSION, 'artifactSHA256': HASHES[name], 'artifactSHA': HASHES[name]}
            for name in reversed(ARTIFACTS)]


def console(items=None):
    body = '::group::Nexus upload output\n' + json.dumps({'upload_results': rows() if items is None else items}, indent=2)
    return '\n'.join('20:43:53 ' + line for line in body.splitlines()) + '\nFinished: SUCCESS\n'


class NexusLockTests(unittest.TestCase):
    def test_timestamped_json_maps_by_name_and_ignores_sidecars(self):
        items = rows()
        for item in rows():
            for suffix in ['.sha256', '.metadata.json', '.deps.json']:
                items.append({**item, 'artifactUrl': item['artifactUrl'] + suffix,
                              'artifactSHA256': 'f' * 64, 'artifactSHA': 'f' * 64})
        self.assertEqual(parse_upload_hashes(console(items), VERSION), HASHES)

    def test_missing_conflicting_wrong_version_and_invalid_hash_fail(self):
        bad_cases = [rows()[:1], rows() + [{**rows()[0], 'artifactSHA256': 'f' * 64, 'artifactSHA': 'f' * 64}],
                     [{**r, 'version': 'wrong'} for r in rows()],
                     [{**r, 'artifactSHA256': 'a' * 40} for r in rows()],
                     [{**r, 'artifactSHA': 'b' * 64} for r in rows()]]
        for items in bad_cases:
            with self.subTest(items=items), self.assertRaises(ValueError):
                parse_upload_hashes(console(items), VERSION)

    def test_only_nexus_group_json_is_used(self):
        with self.assertRaises(ValueError):
            parse_upload_hashes(json.dumps({'upload_results': rows()}), VERSION)
        with self.assertRaises(ValueError):
            parse_upload_hashes('::group::Nexus upload output\n{"upload_results": [', VERSION)

    def test_exact_lock_edit_preserves_other_bytes_and_maps_hashes(self):
        original = json.dumps({'dependencies': [
            {'group': 'paybybank-ios-plugin', 'library': name, 'version': 'old', 'sha256': '0' * 64, 'other': 1}
            for name in ARTIFACTS] + [{'library': 'unrelated', 'version': 'stay', 'sha256': 'unchanged'}]}, indent=4).replace('\n', '\r\n') + '\r\n'
        expected = original
        for name in ARTIFACTS:
            # Target objects deliberately carry distinct new hashes.
            begin = expected.index('"library": "' + name + '"')
            end = expected.index('}', begin)
            segment = expected[begin:end].replace('"old"', '"' + VERSION + '"').replace('0' * 64, HASHES[name])
            expected = expected[:begin] + segment + expected[end:]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'mobile-ci.lock'
            path.write_bytes(original.encode())
            self.assertEqual(update_lock_from_hashes(path, VERSION, HASHES), HASHES)
            self.assertEqual(path.read_bytes(), expected.encode())
            self.assertEqual(validate_lock(path, VERSION), HASHES)
            self.assertEqual(update_lock_from_hashes(path, VERSION, HASHES), HASHES)
            self.assertEqual(path.read_bytes(), expected.encode())

    def test_invalid_input_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'mobile-ci.lock'
            path.write_text('{}')
            with self.assertRaises(ValueError):
                update_lock_from_hashes(path, VERSION, {ARTIFACTS[0]: 'a' * 64})
            self.assertEqual(path.read_text(), '{}')

    def test_full_current_build_log_is_read_and_success_required(self):
        url = 'https://jenkins.invalid/job/plugin/10/consoleText'
        response = SimpleNamespace(status=200, url=url, text=lambda: console() + '\n' * 40000)
        request = Mock()
        request.get.return_value = response
        browser = SimpleNamespace(context=SimpleNamespace(request=request))
        self.assertEqual(plugin_upload_hashes(browser, 'https://jenkins.invalid/job/plugin/', 10, VERSION), HASHES)
        request.get.assert_called_once_with(url, timeout=300000)
        for response in [SimpleNamespace(status=200, url=url, text=lambda: console().replace('Finished: SUCCESS', 'Finished: FAILURE')),
                         SimpleNamespace(status=200, url='https://login.invalid/', text=lambda: console()),
                         SimpleNamespace(status=403, url=url, text=lambda: console())]:
            request.get.return_value = response
            with self.assertRaises(NeedsAttention):
                plugin_upload_hashes(browser, 'https://jenkins.invalid/job/plugin/', 10, VERSION)

    def test_commit_with_valid_but_wrong_checksum_is_rejected(self):
        before = json.dumps([{'group': 'paybybank-ios-plugin', 'library': name,
                              'version': VERSION, 'sha256': '0' * 64} for name in ARTIFACTS]).encode()
        after = json.dumps([{'group': 'paybybank-ios-plugin', 'library': name,
                             'version': VERSION, 'sha256': HASHES[name]} for name in ARTIFACTS]).encode()
        def blob(root, revision, rel):
            if rel == 'mobile-ci.json':
                return before
            return before if revision == 'base' else after
        with patch('appbuilder.verify.git_blob', side_effect=blob):
            gmx_commit(Path('.'), 'base', VERSION, HASHES)
            with self.assertRaises(ValueError):
                gmx_commit(Path('.'), 'base', VERSION, {name: 'f' * 64 for name in ARTIFACTS})

    def test_icon_diagnostic_reports_version_and_each_checksum_readonly(self):
        from appbuilder import diagnose
        from appbuilder.config import load_config
        config = load_config()
        job = config['jenkins']['plugin_parent'] + 'release_test/'
        calls = []
        class Browser:
            payload = console()
            def __init__(self, *args, **kwargs):
                self.page = SimpleNamespace(url=job)
                self.context = SimpleNamespace(request=SimpleNamespace(get=self.get))
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def visit(self, url, **kwargs):
                calls.append(url)
                self.page.url = url
                return 200
            def build_numbers(self, job):
                return {10}
            def _history_result(self, job, number):
                return 'SUCCESS'
            def _heading_result(self, number):
                return 'SUCCESS'
            def get(self, url, **kwargs):
                calls.append(url)
                return SimpleNamespace(status=200, url=url, text=lambda: self.payload)
        with tempfile.TemporaryDirectory() as td, patch.object(diagnose, 'DATA', Path(td)):
            result = diagnose.run_diagnosis(job + '10/', config=config, factory=Browser, emit=lambda line: None)
            saved = json.loads((Path(td) / 'diagnostics/plugin/last-result.json').read_text())
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['artifact_result']['status'], 'PASS')
        self.assertEqual({row['library']: row['sha256'] for row in result['artifact_result']['artifacts']}, HASHES)
        self.assertTrue(all(row['version'] == VERSION for row in saved['artifact_result']['artifacts']))
        self.assertEqual(calls, [job, job + '10/', job + '10/consoleText'])
        expected = {'version': VERSION, 'journey_sha256': HASHES['PayByBankJourneyPluginKit'],
                    'domainmodel_sha256': HASHES['PayByBankDomainModelPluginKit']}
        for field in (None, 'version', 'journey_sha256', 'domainmodel_sha256'):
            supplied = dict(expected)
            if field:
                supplied[field] = 'wrong' if field == 'version' else 'f' * 64
            with tempfile.TemporaryDirectory() as td, patch.object(diagnose, 'DATA', Path(td)):
                report = diagnose.run_diagnosis(job + '10/', config=config, factory=Browser,
                                               expected_artifacts=supplied, emit=lambda line: None)
            self.assertEqual(report['verdict'], 'FAIL' if field else 'PASS')
            self.assertEqual(report['status_verdict'], 'PASS')
            self.assertEqual(len(report['comparisons']), 3)
            self.assertEqual([item['field'] for item in report['comparisons'] if not item['match']], [field] if field else [])

        Browser.payload = 'unrelated log\n' * 100000 + console().replace('Finished: SUCCESS\n', '') + 'trailing diagnostic\n' * 100000
        with tempfile.TemporaryDirectory() as td, patch.object(diagnose, 'DATA', Path(td)):
            report = diagnose.run_diagnosis(job + '10/', config=config, factory=Browser,
                                           expected_artifacts=expected, emit=lambda line: None)
        self.assertEqual(report['verdict'], 'PASS')
        trace = '\n'.join(report['logs'])
        self.assertIn('Nexus upload output 分组=1', trace)
        self.assertIn('upload_results 条目=2', trace)
        self.assertIn('[DOWNLOAD] 完整响应已读取', trace)
        self.assertIn('结束标记=NOT FOUND', trace)
        self.assertNotIn('跳过长 Console', trace)

    def test_invalid_expectations_fail_before_browser_starts(self):
        from appbuilder.diagnose import validate_artifact_expectations
        for values in ({}, {'version': VERSION}, {'version': VERSION, 'journey_sha256': 'bad', 'domainmodel_sha256': '1' * 64}):
            with self.assertRaises(ValueError):
                validate_artifact_expectations(values)


    def test_timestamped_final_status_without_accepting_earlier_success(self):
        from appbuilder.jenkins import final_result_from_console
        for prefix in ('20:43:53 ', '[20:43:53.123] ', '\x1b[32m20:43:53 '):
            self.assertEqual(final_result_from_console(prefix + 'Finished: SUCCESS\x1b[0m\n'), 'SUCCESS')
        self.assertIsNone(final_result_from_console('20:43:53 Finished: SUCCESS\n20:43:54 more work'))

    def test_missing_marker_requires_independent_success_in_formal_flow(self):
        url = 'https://jenkins.invalid/job/plugin/10/consoleText'
        payload = console().replace('Finished: SUCCESS\n', '') + 'post-build output\n'
        request = Mock()
        request.get.return_value = SimpleNamespace(status=200, url=url, text=lambda: payload)
        browser = SimpleNamespace(context=SimpleNamespace(request=request))
        with self.assertRaises(NeedsAttention):
            plugin_upload_hashes(browser, 'https://jenkins.invalid/job/plugin/', 10, VERSION)
        self.assertEqual(plugin_upload_hashes(browser, 'https://jenkins.invalid/job/plugin/', 10, VERSION, confirmed_success=True), HASHES)
        request.get.return_value.text = lambda: console().replace('Finished: SUCCESS', 'Finished: FAILURE')
        with self.assertRaises(NeedsAttention):
            plugin_upload_hashes(browser, 'https://jenkins.invalid/job/plugin/', 10, VERSION, confirmed_success=True)

    def test_html_or_partial_log_is_rejected_and_download_is_traced(self):
        from appbuilder.jenkins import read_full_console
        url = 'https://jenkins.invalid/job/plugin/10/consoleText'
        request = Mock()
        browser = SimpleNamespace(context=SimpleNamespace(request=request))
        for headers, body in [({'content-type': 'text/html'}, '<html>login</html>'),
                              ({'x-more-data': 'true'}, console())]:
            request.get.return_value = SimpleNamespace(status=200, url=url, headers=headers, text=lambda: body)
            logs = []
            with self.assertRaises(NeedsAttention):
                read_full_console(browser, 'https://jenkins.invalid/job/plugin/', 10, emit=logs.append)
            self.assertTrue(any(url in line for line in logs))
            self.assertTrue(any('bytes=' in line for line in logs))
