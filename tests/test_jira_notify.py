import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import jira_notify as J
from appbuilder import local_llm as L

PATTERN = J.DEFAULT_KEY


class FakeLLM:
    def __init__(self, answer=None):
        self.answer = answer

    def ask_json(self, system, user):
        return self.answer


class FakeJira:
    def __init__(self, status='Dev Done', existing_comments=()):
        self.calls = []
        self.status = status
        self.existing = list(existing_comments)

    def issue(self, key):
        return {'fields': {'reporter': {'name': 'alice', 'displayName': 'Alice'}, 'status': {'name': self.status}}}

    def comments(self, key):
        return [{'body': c} for c in self.existing]

    def add_comment(self, key, text):
        self.calls.append(('comment', key, text))

    def assign(self, key, reporter):
        self.calls.append(('assign', key, reporter['name']))

    def transition(self, key, name):
        self.calls.append(('transition', key, name))


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                   check=True, capture_output=True)


class ExtractionTests(unittest.TestCase):
    def commits(self, *subjects):
        return [{'sha': str(i), 'subject': s, 'body': ''} for i, s in enumerate(subjects)]

    def test_subject_key_beats_body_and_unrelated_text_is_ignored(self):
        c = [{'sha': '1', 'subject': 'PBB-12 fix title', 'body': 'see PBB-99'},
             {'sha': '2', 'subject': 'chore: bump', 'body': ''},
             {'sha': '3', 'subject': 'fix', 'body': 'Refs: ABC-7'}]
        found, amb = J.tickets_from_commits(c, PATTERN)
        self.assertEqual(sorted(found), ['ABC-7', 'PBB-12'])
        self.assertEqual(amb, [])

    def test_several_keys_without_model_is_skipped_not_guessed(self):
        found, amb = J.tickets_from_commits(self.commits('PBB-1 PBB-2 combined'), PATTERN)
        self.assertEqual(found, {})
        self.assertEqual(len(amb), 1)

    def test_model_choice_is_accepted_only_if_it_is_a_candidate(self):
        c = self.commits('PBB-1 PBB-2 combined')
        found, _ = J.tickets_from_commits(c, PATTERN, FakeLLM({'key': 'PBB-2'}))
        self.assertEqual(list(found), ['PBB-2'])
        found, amb = J.tickets_from_commits(c, PATTERN, FakeLLM({'key': 'PBB-999'}))
        self.assertEqual(found, {})
        self.assertEqual(len(amb), 1)

    def test_summary_falls_back_when_model_missing_or_returns_junk(self):
        self.assertEqual(L.tester_summary(None, 'PBB-1 fix'), 'PBB-1 fix')
        self.assertEqual(L.tester_summary(FakeLLM({'summary': 'x'}), 'PBB-1 fix'), 'PBB-1 fix')
        self.assertIn('支付', L.tester_summary(FakeLLM({'summary': '修复支付页标题显示，请验证标题文案'}), 'PBB-1 fix'))

    def test_missing_model_file_means_no_ai(self):
        llm = L.LocalLLM('/nonexistent/model.gguf')
        self.assertFalse(llm.available())
        self.assertIsNone(llm.ask_json('s', 'u'))
        self.assertIsNone(L.get_llm({'ai': {'enabled': False, 'model_path': 'x'}}))


class GitAndFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'plugin'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'develop')
        for msg in ('PBB-1 first', 'PBB-2 second', 'no key here'):
            (self.repo / 'f.txt').write_text(msg)
            git(self.repo, 'add', '.')
            git(self.repo, 'commit', '-q', '-m', msg)
        self.shas = subprocess.run(['git', '-C', str(self.repo), 'log', '--format=%H', '--reverse'],
                                   capture_output=True, text=True).stdout.split()

    def tearDown(self):
        self.tmp.cleanup()

    def test_commits_between_returns_only_new_range(self):
        rows = J.commits_between(self.repo, self.shas[0], self.shas[2])
        self.assertEqual([r['subject'] for r in rows], ['no key here', 'PBB-2 second'])
        self.assertEqual(J.commits_between(self.repo, None, self.shas[2]), [])

    def run_notify(self, jira_conf, ledger=None, client=None, status='SUCCESS', share='https://tf/app'):
        data = Path(self.tmp.name) / 'data'
        data.mkdir()
        ledger_path = data / 'jira_ledger.json'
        if ledger is not None:
            ledger_path.write_text(json.dumps(ledger))
        patched = {}
        row = {'status': status, 'params': json.dumps({'plugin_source': 'develop', 'gmx_job': 'https://j/1'})}
        state = {'runtime': {'plugin_source_sha': self.shas[2], 'gmx_build_number': 7,
                             'testfairy_app': {'share_url': share}}}
        config = {'jira': {'enabled': True, 'base_url': 'https://jira.example.com', **jira_conf}}
        with patch.object(J, 'LEDGER', ledger_path), patch.object(J, 'task', return_value=row), \
                patch.object(J, 'read_state', return_value=state), \
                patch.object(J, 'task_folder', return_value=Path(self.tmp.name)), \
                patch.object(J, 'patch_runtime', side_effect=lambda tid, **kw: patched.update(kw)):
            J.notify_jira('T1', client=client, llm=None, config=config)
        return patched.get('jira_report'), (json.loads(ledger_path.read_text()) if ledger_path.exists() else {})

    def test_first_run_only_records_baseline_and_touches_nothing(self):
        jira = FakeJira()
        report, ledger = self.run_notify({'dry_run': False}, client=jira)
        self.assertEqual(report['status'], 'BASELINE')
        self.assertEqual(jira.calls, [])
        self.assertEqual(ledger['develop']['last_sha'], self.shas[2])

    def test_real_run_comments_assigns_transitions_and_advances_ledger(self):
        jira = FakeJira()
        report, ledger = self.run_notify({'dry_run': False}, ledger={'develop': {'last_sha': self.shas[0]}}, client=jira)
        self.assertEqual(report['status'], 'DONE')
        self.assertEqual([c[0] for c in jira.calls], ['comment', 'assign', 'transition'])
        self.assertEqual(jira.calls[0][1], 'PBB-2')
        self.assertIn('https://tf/app', jira.calls[0][2])
        self.assertEqual(jira.calls[2][2], 'In Test')
        self.assertEqual(ledger['develop']['last_sha'], self.shas[2])

    def test_dry_run_changes_nothing_and_keeps_baseline(self):
        jira = FakeJira()
        report, ledger = self.run_notify({'dry_run': True}, ledger={'develop': {'last_sha': self.shas[0]}}, client=jira)
        self.assertEqual(report['status'], 'DRY_RUN')
        self.assertEqual(jira.calls, [])
        self.assertEqual(ledger['develop']['last_sha'], self.shas[0])

    def test_rerun_is_idempotent_for_comment_and_status(self):
        jira = FakeJira(status='In Test', existing_comments=['[app-build:7] old'])
        report, _ = self.run_notify({'dry_run': False}, ledger={'develop': {'last_sha': self.shas[0]}}, client=jira)
        self.assertEqual([c[0] for c in jira.calls], ['assign'])

    def test_failed_build_or_disabled_does_nothing(self):
        jira = FakeJira()
        report, _ = self.run_notify({'dry_run': False}, ledger={'develop': {'last_sha': self.shas[0]}}, client=jira, status='FAILED')
        self.assertIsNone(report)
        self.assertEqual(jira.calls, [])

    def test_client_requires_https_and_credentials(self):
        with self.assertRaises(ValueError):
            J.JiraClient('http://jira.example.com', env={'JIRA_TOKEN': 'x'})
        with self.assertRaises(ValueError):
            J.JiraClient('https://jira.example.com', env={})
        self.assertTrue(J.JiraClient('https://jira.example.com', env={'JIRA_TOKEN': 'x'}).auth.startswith('Bearer '))


if __name__ == '__main__':
    unittest.main()
