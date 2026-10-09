import json
import tempfile
import unittest
from pathlib import Path
from appbuilder.versions import branch_names, job_urls, parse_artifacts, validate_args
from appbuilder.edits import (XCCONFIGS, ARTIFACTS, edit_plugin_versions, update_mobile_ci,
                              validate_lock, ensure_lock_other_entries_unchanged)
from appbuilder.config import load_config


class TestVersions(unittest.TestCase):
    def test_job_name_conventions(self):
        c = load_config()
        b = branch_names('1.3', '202610100030')
        self.assertEqual('release/1.3.202610100030', b['plugin_branch'])
        self.assertEqual('upload/auto202610100030', b['gmx_branch'])
        urls = job_urls(c, b['plugin_branch'], b['gmx_branch'])
        self.assertTrue(urls['plugin_job'].endswith('/job/release_1_3_202610100030/'))
        self.assertTrue(urls['gmx_job'].endswith('/job/upload%2Fauto202610100030/'))

    def test_artifacts(self):
        version = '1.3.1-alpha.1-SNAPSHOT.2'
        paths = [f'build-artifact/build-universal/{n}-{version}.tar.gz' for n in ARTIFACTS]
        paths += ['build-artifact/archive/unrelated.tar.gz', 'build-artifact/build-universal/extra.deps.json']
        self.assertEqual(version, parse_artifacts(paths))
        with self.assertRaises(ValueError):
            parse_artifacts(paths[:1])
        with self.assertRaises(ValueError):
            parse_artifacts([paths[0], paths[1].replace(version, '1.3.999')])

    def test_arguments(self):
        validate_args('1.3', 'develop', 'release/1.0', 'UK', 'MyProject')
        with self.assertRaises(ValueError):
            validate_args('1.3.0', 'develop', 'develop', 'UK', 'MyProject')
        with self.assertRaises(ValueError):
            validate_args('1.3', 'develop', 'develop', 'UK', '../bad')


class TestEdits(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)

    def test_two_xcconfigs_only(self):
        before = b'#include "Keep.h"\r\nBUNDLE_VERSION_SHORT = 1.2.2\r\nBUNDLE_VERSION = 1\r\n'
        for rel in XCCONFIGS:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(before)
        changes = edit_plugin_versions(self.root, '1.3.202610100030')
        self.assertEqual(len(changes), 2)
        for rel in XCCONFIGS:
            actual = (self.root / rel).read_bytes()
            self.assertEqual(actual, before.replace(b'BUNDLE_VERSION_SHORT = 1.2.2', b'BUNDLE_VERSION_SHORT = 1.3.202610100030'))

    def _sample(self):
        return '{\n  "dependencies": [\n' + ',\n'.join(
            '    {"source": "internal", "group": "paybybank-ios-plugin", '
            f'"library": "{n}", "version": "1.2.2"}}' for n in ARTIFACTS) + ',\n' + \
            '    {"group":"other","library":"abc","version":"1.0.0"}\n  ]\n}\n'

    def test_json_minimal_diff(self):
        f = self.root / 'mobile-ci.json'
        before = self._sample()
        f.write_text(before, encoding='utf-8')
        update_mobile_ci(f, '1.3.1-alpha.1-SNAPSHOT.2')
        actual = f.read_text(encoding='utf-8')
        self.assertEqual(before.replace('"version": "1.2.2"', '"version": "1.3.1-alpha.1-SNAPSHOT.2"'), actual)
        self.assertEqual(json.loads(actual)['dependencies'][-1]['version'], '1.0.0')

    def test_lock_and_semantic_guard(self):
        f = self.root / 'mobile-ci.lock'
        before = self._sample().replace('"version": "1.2.2"', '"version": "1.3.1-alpha.1-SNAPSHOT.2"')
        data = json.loads(before)
        for package in data['dependencies'][:2]:
            package['sha256'] = 'a' * 64
        f.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(len(validate_lock(f, '1.3.1-alpha.1-SNAPSHOT.2')), 2)
        ensure_lock_other_entries_unchanged(self._sample(), f.read_text())
        data['dependencies'][-1]['version'] = '2.0.0'
        with self.assertRaises(ValueError):
            ensure_lock_other_entries_unchanged(self._sample(), json.dumps(data))


if __name__ == '__main__':
    unittest.main()
