import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from appbuilder.testfairy_trace import AttemptTrace


class AttemptTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_files_events_and_private_permissions(self):
        trace = AttemptTrace(self.root)
        trace.event('folder', 'start', remaining_seconds=12, counts={'visible': 2}, page_cursor=3)
        trace.event('folder', 'end', reason='directory selected')
        records = [json.loads(line) for line in trace.log_path.read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['start', 'end'])
        self.assertEqual(records[0]['counts']['visible'], 2)
        self.assertEqual(records[0]['remaining_seconds'], 12)
        self.assertGreaterEqual(records[1]['elapsed_seconds'], records[0]['elapsed_seconds'])
        self.assertGreaterEqual(records[1]['duration_seconds'], 0)
        for directory in [trace.directory, trace.directory.parent]:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for file in [trace.log_path, trace.evidence_path]:
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertIsNone(trace.summary['first_error'])

    def test_recursive_redaction_and_no_raw_browser_evidence(self):
        trace = AttemptTrace(self.root, secrets=('fake-user', 'S P A C E secret'))
        trace.event('cards', 'end', payload={
            'note': 'fake-user S P A C E secret', 'nested': [{'password': 'nobodyknows', 'cookie': 'value'}],
            'url': 'https://alice:pass@host.invalid/a?ticket=SHOULD_NOT_SAVE#PRIVATE',
            'html': '<div>SECRET DOM</div>', 'unexpected': '<span>hidden</span>',
            'headers': {'custom': 'hidden-header'}, 'object': object()})
        trace.failure('cards', ValueError('password="another secret"; fake-user'), {'dom': 'hidden-dom'})
        persisted = trace.log_path.read_text() + trace.evidence_path.read_text()
        for forbidden in ['fake-user', 'S P A C E secret', 'nobodyknows', 'alice', 'SHOULD_NOT_SAVE',
                          'PRIVATE', 'SECRET DOM', 'hidden-header', 'hidden-dom', 'another secret']:
            self.assertNotIn(forbidden, persisted)
        record = json.loads(trace.log_path.read_text().splitlines()[0])
        self.assertEqual(record['payload']['url'], 'https://host.invalid [path omitted]')
        self.assertEqual(record['payload']['html'], '[OMITTED]')
        self.assertEqual(record['payload']['unexpected'], '[OMITTED: markup]')

    def test_first_error_and_evidence_survive_later_timeout(self):
        trace = AttemptTrace(self.root)
        trace.failure('folder', ValueError('directory absent'), {'visible_names': ['APPS'], 'count': 0})
        before = trace.evidence_path.read_text()
        trace.failure('wait', TimeoutError('deadline expired'), {'count': 4})
        self.assertEqual(trace.summary['first_error']['phase'], 'folder')
        self.assertEqual(trace.summary['first_error']['type'], 'ValueError')
        self.assertEqual(trace.evidence_path.read_text(), before)
        rows = [json.loads(line) for line in trace.log_path.read_text().splitlines()]
        self.assertEqual(rows[-1]['error']['type'], 'TimeoutError')
        self.assertEqual(rows[-1]['first_error']['message'], 'directory absent')

    def test_write_failure_is_explicit_without_masking_original(self):
        trace = AttemptTrace(self.root, secrets=('secret-file',))
        with patch.object(trace, '_write', side_effect=OSError('permission secret-file')):
            trace.failure('match', ValueError('original match failed'))
            trace.event('wait', 'end')
        self.assertEqual(trace.summary['first_error']['message'], 'original match failed')
        self.assertEqual(trace.summary['logging_error']['type'], 'OSError')
        self.assertNotIn('secret-file', json.dumps(trace.summary))

    def test_unwritable_initial_path_still_keeps_first_error(self):
        blocker = self.root / 'file'
        blocker.write_text('not a directory')
        trace = AttemptTrace(blocker)
        trace.failure('login', RuntimeError('normal login failed'))
        self.assertIn('logging_error', trace.summary)
        self.assertEqual(trace.summary['first_error']['phase'], 'login')

    def test_bounded_json_and_independent_attempts(self):
        one, two = AttemptTrace(self.root), AttemptTrace(self.root)
        self.assertNotEqual(one.attempt_id, two.attempt_id)
        one.event('cards', 'end', candidates=[{'description': 'a' * 4000} for _ in range(1000)])
        self.assertLessEqual(one.log_path.stat().st_size, 16384)
        self.assertTrue(json.loads(one.log_path.read_text())['truncated'])
        self.assertEqual(two.log_path.read_text(), '')

    def test_expected_actual_build_identity_preserved_with_safe_urls(self):
        trace = AttemptTrace(self.root)
        trace.event('match', 'end', expected={'build_url':'https://jenkins.invalid/job/gmx/42/?token=secret',
                                            'number':42,'sha':'abcdef','folder':'IOS_Upload/IOS_Develop/App'},
                    actual={'number':41,'sha':'different'}, remaining_seconds=0)
        row = json.loads(trace.log_path.read_text())
        self.assertEqual(row['expected']['build_url'], 'https://jenkins.invalid/job/gmx/42/')
        self.assertEqual(row['expected']['folder'], 'IOS_Upload/IOS_Develop/App')
        self.assertEqual(row['actual']['number'], 41)

    def test_bad_encoding_and_writer_errors_cannot_mask_original(self):
        trace = AttemptTrace(self.root)
        trace.event('folder', 'start', reason='invalid\ud800text')
        self.assertIn('invalid?text', trace.log_path.read_text())
        with patch.object(trace, '_write', side_effect=ValueError('writer closed')):
            trace.failure('cards', TimeoutError('original timeout'))
        self.assertEqual(trace.summary['first_error']['type'], 'TimeoutError')
        self.assertEqual(trace.summary['logging_error']['type'], 'ValueError')

    def test_download_paths_never_persist_even_in_errors_or_build_url_disguise(self):
        trace = AttemptTrace(self.root)
        trace.event('inspect', 'end', download_url='https://files.invalid/private/PATH_SIGNATURE/app.ipa?token=q',
                    note='failed opening https://files.invalid/private/OTHER_SIGNATURE/app.ipa#f',
                    expected={'build_url':'https://jenkins.invalid/job/feature%2Fbranch/17/?token=q'},
                    invalid={'build_url':'https://files.invalid/download/HIDDEN_SIGNATURE/app.ipa'},
                    insecure={'build_url':'http://jenkins.invalid/job/NAME_SIGNATURE/17/'})
        trace.failure('download', ValueError('https://files.invalid/ERROR_SIGNATURE.ipa'),
                      {'url':'https://files.invalid/EVIDENCE_SIGNATURE.zip'})
        persisted=trace.log_path.read_text()+trace.evidence_path.read_text()
        for secret in ['PATH_SIGNATURE','OTHER_SIGNATURE','HIDDEN_SIGNATURE','NAME_SIGNATURE',
                       'ERROR_SIGNATURE','EVIDENCE_SIGNATURE','token=q']:
            self.assertNotIn(secret,persisted)
        row=json.loads(trace.log_path.read_text().splitlines()[0])
        self.assertEqual(row['expected']['build_url'],'https://jenkins.invalid/job/feature%2Fbranch/17/')
        self.assertEqual(row['download_url'],'https://files.invalid [path omitted]')
        self.assertEqual(trace.summary['first_error']['stage'],'download')
