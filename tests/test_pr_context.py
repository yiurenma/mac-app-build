"""Whole-PR context for the Copilot tester note, and the "try with one real PR" endpoint."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder import ai_api, ai_setup, copilot_bridge, local_llm, pr_context

PR = {'number': 12, 'title': 'TRANSACT-100: Fix consent account id', 'body': 'Sends the selected account id.'}
COMMITS = [{'commit': {'message': 'TRANSACT-100: send account id\n\nWas sending the debtor id.'}},
           {'commit': {'message': 'Add unit test'}}]
FILES = [{'filename': 'Sources/Consent.swift', 'status': 'modified', 'additions': 3, 'deletions': 1,
          'patch': '@@ -1 +1 @@\n-let id = debtor\n+let id = selected'},
         {'filename': 'Pods/Lib/X.swift', 'status': 'modified', 'additions': 900, 'deletions': 0, 'patch': 'x' * 5000},
         {'filename': 'Package.resolved', 'status': 'modified', 'additions': 1, 'deletions': 1, 'patch': 'pins'},
         {'filename': 'Big.swift', 'status': 'added', 'additions': 400, 'deletions': 0, 'patch': 'y' * 9000}]


def fake_repo_get(path):
    if path.startswith('/pulls/12/commits'):
        return COMMITS
    if path.startswith('/pulls/12/files'):
        return FILES
    if path == '/pulls/12':
        return PR
    raise RuntimeError('HTTP 404')


class FetchTests(unittest.TestCase):
    def test_text_has_commits_files_and_trimmed_diffs(self):
        text, stats = pr_context.fetch(fake_repo_get, 12)
        self.assertIn('- TRANSACT-100: send account id (Was sending the debtor id.)', text)
        self.assertIn('- Add unit test', text)
        self.assertIn('Sources/Consent.swift (modified, +3 -1)', text)
        self.assertIn('+let id = selected', text)
        self.assertEqual((stats['commits'], stats['files'], stats['patched']), (2, 4, 2))
        self.assertNotIn('xxxx', text)                      # vendored code is listed, not dumped
        self.assertNotIn('pins', text)
        self.assertIn('... (cut)', text)                    # Big.swift is trimmed
        self.assertLess(text.count('y'), 1600)

    def test_github_errors_return_what_was_gathered(self):
        def flaky(path):
            if 'commits' in path:
                raise RuntimeError('boom')
            return FILES[:1]
        text, stats = pr_context.fetch(flaky, 12)
        self.assertEqual((stats['commits'], stats['files']), (0, 1))
        self.assertEqual(pr_context.fetch(lambda p: (_ for _ in ()).throw(RuntimeError('x')), 1)[0], '')

    def test_total_size_is_capped(self):
        many = [{'filename': f'f{i}.swift', 'status': 'modified', 'additions': 1, 'deletions': 1, 'patch': 'z' * 1400} for i in range(100)]
        text, _ = pr_context.fetch(lambda p: many if 'files' in p else [], 1)
        self.assertLessEqual(len(text), pr_context.MAX_TOTAL + 20)

    def test_pr_url_parsing(self):
        self.assertEqual(pr_context.parse_pr_url('https://git.example.com/org/repo/pull/12'),
                         ('https://git.example.com/org/repo', 12))
        self.assertEqual(pr_context.parse_pr_url('https://git.example.com/org/repo/pull/12/files')[1], 12)
        for bad in ('', 'http://x/o/r/pull/1', 'https://x/o/r/issues/1', 'https://x/o/r/pull/abc', 'https://x/pull/1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pr_context.parse_pr_url(bad)


class PromptTests(unittest.TestCase):
    def test_small_local_models_keep_the_short_input(self):
        system, user, source = local_llm.tester_note_prompt('T', 'B' * 5000, 'EXTRA', rich=False)
        self.assertNotIn('EXTRA', user)
        self.assertEqual(user.count('B'), 2500)

    def test_rich_models_get_everything_and_the_prompt_says_so(self):
        system, user, source = local_llm.tester_note_prompt('T', 'B' * 5000, 'EXTRA-DIFF', rich=True)
        self.assertIn('EXTRA-DIFF', user)
        self.assertEqual(user.count('B'), 5000)
        self.assertIn('commits, changed files and code changes', system)
        self.assertIn('EXTRA-DIFF', source)

    def test_ticket_numbers_from_the_diff_are_allowed_in_the_note_but_invented_ones_are_not(self):
        class LLM:
            rich = True
            def __init__(self, text): self.text = text
            def ask_json(self, s, u): return {'note': self.text}
        ok = local_llm.tester_note(LLM('Check the consent flow still works for TRANSACT-100 accounts.'), 'Fix', 'body', 'see TRANSACT-100 in code')
        self.assertIsNotNone(ok)
        bad = local_llm.tester_note(LLM('Check the consent flow for TRANSACT-999 accounts today.'), 'Fix', 'body', 'see TRANSACT-100')
        self.assertIsNone(bad)


class TryPrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(ai_setup, 'SETTINGS_FILE', Path(self.tmp.name) / 'ai.json'),
                  patch('appbuilder.config.load_config', return_value={'github': {'plugin_url': 'https://git.example.com/org/repo'}}),
                  patch('appbuilder.config.token_or_error', return_value='tok')):
            p.start(); self.addCleanup(p.stop)
        local_llm._CACHE.clear(); self.addCleanup(local_llm._CACHE.clear)
        self.asked = []

    def run_try(self, url, send=True, features=None, note='Check the consent screen and a full transfer.'):
        ai_setup.save({'enabled': True, 'provider': 'copilot', **(features or {})})
        asked = self.asked

        class Fake(copilot_bridge.CopilotLLM):
            def ask_json(self, system, user):
                asked.append((system, user)); return {'note': note}
        calls = []
        def repo_get(self_, path):
            calls.append(path); return fake_repo_get(path)
        with patch.object(local_llm, 'get_llm', return_value=Fake()), \
             patch('appbuilder.jira_notify.PRLookup.repo_get', repo_get):
            out = ai_api.try_pr(url, send)
        return out, calls

    def test_preview_shows_everything_sent_and_does_not_call_the_ai(self):
        out, calls = self.run_try('https://git.example.com/org/repo/pull/12', send=False)
        self.assertEqual(self.asked, [])
        self.assertNotIn('tester_note', out)
        self.assertEqual(out['to'], 'VS Code Copilot')
        self.assertTrue(out['full_context'])
        self.assertEqual((out['stats']['commits'], out['stats']['files']), (2, 4))
        for needle in ('Sends the selected account id.', 'Sources/Consent.swift', '+let id = selected', 'send account id'):
            self.assertIn(needle, out['sent'])

    def test_try_sends_the_same_text_and_returns_the_note(self):
        out, _ = self.run_try('https://git.example.com/org/repo/pull/12')
        self.assertEqual(out['tester_note'], 'Check the consent screen and a full transfer.')
        self.assertEqual(len(self.asked), 1)
        system, user = self.asked[0]
        self.assertIn('+let id = selected', user)
        self.assertTrue(out['sent'].startswith(system[:50]))

    def test_switching_the_full_context_off_sends_title_and_description_only(self):
        out, calls = self.run_try('https://git.example.com/org/repo/pull/12', send=False, features={'pr_context': False})
        self.assertFalse(out['full_context'])
        self.assertNotIn('Sources/Consent.swift', out['sent'])
        self.assertEqual(calls, ['/pulls/12'])

    def test_only_the_configured_github_host_is_contacted(self):
        with self.assertRaisesRegex(ValueError, 'git.example.com'):
            self.run_try('https://evil.example.org/org/repo/pull/12')

    def test_bad_url_and_disabled_ai_are_clear_errors(self):
        with self.assertRaises(ValueError):
            self.run_try('not a url')
        ai_setup.save({'enabled': False})
        with patch('appbuilder.config.load_config', return_value={'github': {'plugin_url': 'https://git.example.com/org/repo'}}):
            with self.assertRaisesRegex(ValueError, '没有启用'):
                ai_api.try_pr('https://git.example.com/org/repo/pull/12')

    def test_endpoint_returns_400_for_user_errors(self):
        code, body = ai_api.handle_post('/api/ai/try-pr', {'url': 'nonsense'})
        self.assertEqual(code, 400)
        self.assertIn('PR', body['error'])


if __name__ == '__main__':
    unittest.main()
