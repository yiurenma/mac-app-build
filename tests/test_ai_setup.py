"""Offline AI: one-click setup, the checks on what the model writes, and where it is used."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder import ai_api as API
from appbuilder import ai_setup as S
from appbuilder import jira_mail as M
from appbuilder import jira_notify as J
from appbuilder import local_llm as L


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for name, value in (('SETTINGS_FILE', self.dir / 'ai_settings.json'), ('MODEL_DIR', self.dir / 'models')):
            p = patch.object(S, name, value); p.start(); self.addCleanup(p.stop)

    def gguf(self, name='m.gguf', size=301 * 1024 * 1024):
        path = self.dir / name
        with path.open('wb') as f:
            f.write(b'GGUF')
            f.truncate(size)
        return path


class SettingsTests(Tmp):
    def test_page_settings_override_config_and_features_default_on(self):
        self.assertFalse(S.feature_on({'ai': {'enabled': False}}, 'tester_note'))
        S.save({'enabled': True, 'model_path': '/m.gguf', 'model': 'phi4-mini'})
        ai = S.effective({'ai': {'enabled': False, 'n_ctx': 2048}})
        self.assertEqual((ai['enabled'], ai['model_path'], ai['n_ctx'], ai['tester_note']), (True, '/m.gguf', 2048, True))
        S.save({'failure_hint': False})
        self.assertFalse(S.feature_on({}, 'failure_hint'))
        self.assertTrue(S.feature_on({}, 'tester_note'))

    def test_bad_values_in_the_file_are_ignored(self):
        S.SETTINGS_FILE.write_text(json.dumps({'enabled': 'yes', 'model': 'evil', 'model_path': 5}))
        self.assertEqual(S.saved(), {})

    def test_model_file_must_be_a_complete_gguf(self):
        self.assertEqual(S.check_model_file(self.gguf()), self.dir / 'm.gguf')
        small = self.gguf('s.gguf', 1024)
        bad = self.gguf('b.gguf')
        bad.write_bytes(b'NOPE' + bad.read_bytes()[4:16])
        for path, message in ((self.dir / 'none.gguf', '找不到'), (small, '太小'), (self.dir / 'x.bin', '找不到')):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                S.check_model_file(path)
        big_bad = self.gguf('c.gguf')
        with big_bad.open('r+b') as f:
            f.write(b'NOPE')
        with self.assertRaisesRegex(ValueError, 'GGUF'):
            S.check_model_file(big_bad)


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DownloadTests(Tmp):
    def listing(self, data, sha=None):
        return json.dumps([{'path': 'README.md', 'size': 10},
                           {'path': 'microsoft_Phi-4-mini-instruct-Q4_K_M.gguf',
                            'lfs': {'size': len(data), 'oid': sha or hashlib.sha256(data).hexdigest()}}]).encode()

    def test_download_checks_sha256_and_resumes(self):
        data = b'GGUF' + bytes(range(256)) * 40
        calls = []

        def fake_open(url, headers=None, timeout=60):
            calls.append((url, headers))
            if '/api/models/' in url:
                return FakeResponse(self.listing(data))
            start = int(headers['Range'].split('=')[1].rstrip('-')) if headers else 0
            return FakeResponse(data[start:], 206 if start else 200)
        S.MODEL_DIR.mkdir(parents=True)
        (S.MODEL_DIR / 'microsoft_Phi-4-mini-instruct-Q4_K_M.gguf.part').write_bytes(data[:100])
        with patch.object(S, '_open', fake_open):
            path = S.download_model('phi4-mini')
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(calls[1][1], {'Range': 'bytes=100-'})
        self.assertIn('/bartowski/microsoft_Phi-4-mini-instruct-GGUF/resolve/main/', calls[1][0])

    def test_wrong_checksum_is_deleted_and_missing_checksum_is_refused(self):
        data = b'GGUF' + b'x' * 1000
        with patch.object(S, '_open', lambda url, headers=None, timeout=60: FakeResponse(
                self.listing(data, 'a' * 64) if '/api/' in url else data)):
            with self.assertRaisesRegex(RuntimeError, '校验不一致'):
                S.download_model('phi4-mini')
        self.assertEqual(list(S.MODEL_DIR.iterdir()), [])
        with patch.object(S, '_open', lambda url, headers=None, timeout=60: FakeResponse(self.listing(data, 'none'))):
            with self.assertRaisesRegex(RuntimeError, '校验值'):
                S.download_model('phi4-mini')

    def test_start_validates_before_running(self):
        with self.assertRaisesRegex(ValueError, '未知的模型'):
            S.start('download', 'gpt-x')
        with self.assertRaisesRegex(ValueError, '找不到'):
            S.start('file', str(self.dir / 'none.gguf'))

    def test_install_runs_runtime_model_and_self_test_then_enables(self):
        path = self.gguf()
        with patch.object(S, 'install_runtime'), patch.object(S, 'download_model', return_value=path), \
                patch.object(S, 'self_test') as test:
            S._run('download', 'qwen3-4b')
        test.assert_called_once_with(path)
        self.assertEqual(S.saved(), {'enabled': True, 'model_path': str(path), 'model': 'qwen3-4b'})
        self.assertEqual(S._JOB['finished'], 'ok')

    def test_failed_install_keeps_ai_off_and_says_why(self):
        with patch.object(S, 'install_runtime', side_effect=RuntimeError('运行库安装失败（需要 Xcode）')):
            S._run('download', 'phi4-mini')
        self.assertEqual(S.saved(), {})
        self.assertIn('Xcode', S._JOB['error'])


class ApiTests(Tmp):
    def test_cannot_enable_without_a_model_and_switches_save(self):
        code, body = API.handle_post('/api/ai/settings', {'enabled': True})
        self.assertEqual(code, 400)
        S.save({'model_path': str(self.gguf())})
        with patch.object(S, 'runtime_version', return_value='0.3.9'):
            code, body = API.handle_post('/api/ai/settings', {'enabled': True, 'failure_hint': False, 'x': 1})
        self.assertEqual(code, 200)
        self.assertEqual((body['enabled'], body['features']['failure_hint'], body['model_ok']), (True, False, True))
        self.assertNotIn('x', S.saved())
        self.assertIsNone(API.handle_post('/api/other', {}))


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def ask_json(self, system, user):
        self.asked.append(user)
        return self.answer


class CheckTests(unittest.TestCase):
    def test_only_short_plain_english_without_new_ticket_numbers_is_used(self):
        src = 'TRANSACT-1 fix consent param'
        self.assertEqual(L.clean_sentence(' "Check the consent screen sends the account id." ', src),
                         'Check the consent screen sends the account id.')
        for bad in ('请检查同意页面的参数是否正确', 'See https://x.example for details', 'Fix *bold* here now',
                    'Also fixes TRANSACT-99 for the consent screen', 'ok', 'word ' * 60, None, 5):
            with self.subTest(bad=bad):
                self.assertIsNone(L.clean_sentence(bad, src))
        self.assertIsNotNone(L.clean_sentence('TRANSACT-1 now sends the account id.', src))

    def test_tester_note_and_failure_hint_use_the_checks(self):
        self.assertEqual(L.tester_note(FakeLLM({'note': 'Check that the consent call sends the account id.'}), 'Fix consent'),
                         'Check that the consent call sends the account id.')
        self.assertIsNone(L.tester_note(FakeLLM({'note': '检查'}), 'Fix consent'))
        self.assertIsNone(L.tester_note(None, 'Fix consent'))
        hint = L.failure_hint(FakeLLM({'cause': 'The signing certificate is missing on the agent.',
                                       'fix': 'Ask the Jenkins admin to reinstall the distribution certificate.'}),
                              'GMX build', 'Jenkins build #2 finished with result FAILURE.', ['No signing certificate'])
        self.assertEqual(hint['cause'], 'The signing certificate is missing on the agent.')
        self.assertIsNone(L.failure_hint(FakeLLM({'cause': 'x'}), 'GMX build', 'r', []))

    def test_qwen3_is_asked_to_answer_directly(self):
        class Model:
            def create_chat_completion(self, messages, **kw):
                self.user = messages[1]['content']
                return {'choices': [{'message': {'content': '{"ok": true}'}}]}
        llm = L.LocalLLM('/m/Qwen3-4B-Q4_K_M.gguf')
        llm._model = Model()
        self.assertEqual(llm.ask_json('s', 'u'), {'ok': True})
        self.assertTrue(llm._model.user.endswith('/no_think'))


class UseTests(unittest.TestCase):
    def test_failure_reasons_become_english(self):
        cases = {'GMX 正式构建 #41 失败: FAILURE (https://j/x/41/)': 'Jenkins build #41 finished with result FAILURE.',
                 'Plugin Callback #7 失败: ABORTED': 'The Plugin callback build #7 finished with result ABORTED.',
                 'GMX 仍停留在认证页面，可能是密码错误或额外验证': 'Jenkins sign-in is needed',
                 'TestFairy 会话已失效，停止查找，构建不会重跑': 'The App link could not be found',
                 'Worker 未启动；可在恢复接收后 Resume': 'APP Builder stopped',
                 'Already English reason': 'Already English reason',
                 '完全未知的中文原因': ''}
        for note, expected in cases.items():
            with self.subTest(note=note):
                self.assertTrue(M.english_reason(note).startswith(expected) if expected else M.english_reason(note) == '')

    def test_tester_note_goes_under_the_box_in_the_comment(self):
        text = J.comment_text('[app-build:T]', 'https://tf/a', 2, 'release/v1', [], '', '', 'Check the consent screen.')
        lines = text.split('\n')
        self.assertEqual(lines[lines.index('{panel}') + 1], '*For testers:* Check the consent screen.')

    def test_hint_and_note_show_in_the_emails(self):
        row = {'id': 'T1', 'stage': 'GMX_JENKINS', 'status': 'FAILED', 'note': 'GMX 正式构建 #2 失败: FAILURE'}
        params = {'country': 'UK', 'project': 'PBB', 'plugin_branch': 'release/v1'}
        hint = {'cause': 'Signing certificate missing.', 'fix': 'Reinstall it on the agent.'}
        with patch.object(M, '_log_tail', return_value=[]):
            _, text = M.compose_failure(row, params, {}, [], hint=hint)
            html = M.failure_html(row, params, {}, [], hint=hint)
        self.assertIn('Reason: Jenkins build #2 finished with result FAILURE.', text)
        self.assertIn('Likely cause (offline AI suggestion): Signing certificate missing.', text)
        self.assertIn('offline AI suggestion', html)
        self.assertIn('<b>Try:</b> Reinstall it on the agent.', html)
        runtime = {'gmx_build_number': 2, 'testfairy_app': {'share_url': 'https://tf/a'},
                   'jira_report': {'status': 'DONE', 'tickets': [{'key': 'A-1', 'summary': 'S', 'tester_note': 'Check X.'}]}}
        html = M.success_html(row, params, runtime, 'https://jira')
        self.assertIn('<b>For testers:</b> Check X.', html)
        self.assertIn('For testers: Check X.', M.compose(row, params, runtime, 'https://jira')[1])


if __name__ == '__main__':
    unittest.main()
