"""Regression cases reconstructed from IMG_0726; not a full live log.

The screenshot proves the basename/JSON version difference. ISO timestamps
are a raw-console compatibility case; their presence in the live response
has not yet been verified.
"""
import json
import unittest
from appbuilder.nexus import parse_upload_metadata, parse_upload_hashes
from appbuilder.versions import ARTIFACTS

BASE = '1.3.202610101332'
FULL = BASE + '-SNAPSHOT.2'
JOURNEY = '178f82cf4b818f674c2238085e286e917c3704286a9c3a736c80a2c8438c9df3'
# DomainModel checksum is synthetic; its tarball value is not in this photo.
HASHES = {ARTIFACTS[0]: 'd' * 64, ARTIFACTS[1]: JOURNEY}


def payload(prefix='14:33:40 ', version=BASE, marker='::group::Nexus upload output'):
    rows = []
    for name in ARTIFACTS:
        row = {'artifactUrl': f'https://nexus.invalid/plugin/{BASE}/{name}-{FULL}.tar.gz',
               'version': version, 'artifactSHA256': HASHES[name], 'artifactSHA': HASHES[name]}
        rows.extend([{**row, 'artifactUrl': row['artifactUrl'] + '.metadata.json',
                      'artifactSHA256': 'a' * 64, 'artifactSHA': 'a' * 64}, row])
    text = marker + '\n' + json.dumps({'upload_results': rows}, indent=2) + '\n::endgroup::\nFinished: SUCCESS'
    return '\n'.join(prefix + line for line in text.splitlines())


class FieldFormatTests(unittest.TestCase):
    def test_screenshot_base_version_and_full_tarball_version(self):
        self.assertEqual(parse_upload_metadata(payload()), {'version': FULL, 'hashes': HASHES})
        self.assertEqual(parse_upload_hashes(payload(), FULL), HASHES)

    def test_raw_console_timestamp_variants(self):
        for prefix in ('[2026-10-10T06:33:40.123Z] ',
                       '2026-10-10T14:33:40+08:00 ',
                       '[2026-10-10 14:33:40.123+08:00] ',
                       '\x1b[32m[2026-10-10T06:33:40.123Z]\x1b[0m ',
                       '[14:33:40.123] '):
            with self.subTest(prefix=prefix):
                self.assertEqual(parse_upload_metadata(payload(prefix)), {'version': FULL, 'hashes': HASHES})

    def test_wrong_base_version_still_rejected(self):
        with self.assertRaises(ValueError):
            parse_upload_metadata(payload(version='1.3.202610101333'))

    def test_wrong_full_version_still_rejected(self):
        with self.assertRaises(ValueError):
            parse_upload_hashes(payload(), BASE + '-SNAPSHOT.3')

    def test_marker_echo_not_accepted(self):
        with self.assertRaises(ValueError):
            parse_upload_metadata(payload(marker='+ echo "::group::Nexus upload output"'))

    def test_unrecognized_marker_has_actionable_diagnostic(self):
        messages = []
        with self.assertRaises(ValueError):
            parse_upload_metadata(payload(prefix='[unsupported] '), emit=messages.append)
        self.assertTrue(any('未识别' in msg for msg in messages), messages)

    def test_final_result_uses_same_timestamp_normalization(self):
        from appbuilder.jenkins import final_result_from_console
        self.assertEqual(final_result_from_console('[2026-10-10T06:33:40.123Z] Finished: SUCCESS\n'), 'SUCCESS')

if __name__ == '__main__':
    unittest.main()
