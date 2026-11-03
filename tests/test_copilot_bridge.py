"""Python side of the VS Code Copilot bridge: client, settings, installer. No VS Code needed."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from appbuilder import ai_api, ai_setup, copilot_bridge, local_llm, vscode_ext


class FakeBridge:
    """Speaks the extension's protocol: bearer token, /health, /ask."""
    def __init__(self, reply):
        self.reply, self.seen = reply, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer tok': return self._send(401, {'error': 'unauthorised'})
                self._send(200, {'ok': True})
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer tok': return self._send(401, {'error': 'unauthorised'})
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.seen.append(body)
                code, payload = outer.reply(body)
                self._send(code, payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def info_file(self, directory, pid=None, token='tok'):
        path = Path(directory) / 'bridge.json'
        path.write_text(json.dumps({'port': self.server.server_port, 'token': token, 'pid': pid or os.getpid()}))
        return path

    def close(self):
        self.server.shutdown(); self.server.server_close()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def bridge(self, reply):
        b = FakeBridge(reply); self.addCleanup(b.close)
        return b, b.info_file(self.tmp.name)

    def test_ask_json_parses_the_answer_and_sends_both_prompts(self):
        b, info = self.bridge(lambda body: (200, {'text': 'Sure: {"note": "Check login"} done'}))
        llm = copilot_bridge.CopilotLLM(info=info)
        self.assertTrue(llm.available())
        self.assertEqual(llm.ask_json('SYS', 'USER'), {'note': 'Check login'})
        self.assertEqual(b.seen, [{'system': 'SYS', 'user': 'USER'}])

    def test_every_failure_returns_none_never_raises(self):
        for code, payload in ((503, {'error': 'no model'}), (403, {'error': 'permission'}), (500, {'error': 'internal error'})):
            with self.subTest(code=code):
                b, info = self.bridge(lambda body, c=code, p=payload: (c, p))
                self.assertIsNone(copilot_bridge.CopilotLLM(info=info).ask_json('s', 'u'))
        b, info = self.bridge(lambda body: (200, {'text': 'no json here'}))
        self.assertIsNone(copilot_bridge.CopilotLLM(info=info).ask_json('s', 'u'))
        b, info = self.bridge(lambda body: (200, {'text': '[1, 2]'}))
        self.assertIsNone(copilot_bridge.CopilotLLM(info=info).ask_json('s', 'u'))

    def test_missing_stale_or_wrong_token_is_reported_not_raised(self):
        missing = Path(self.tmp.name) / 'nope.json'
        self.assertFalse(copilot_bridge.check(missing)['ok'])
        self.assertIsNone(copilot_bridge.CopilotLLM(info=missing).ask_json('s', 'u'))
        b = FakeBridge(lambda body: (200, {'text': '{}'})); self.addCleanup(b.close)
        wrong = b.info_file(self.tmp.name, token='bad')
        self.assertFalse(copilot_bridge.check(wrong)['ok'])
        gone = Path(self.tmp.name) / 'gone.json'
        gone.write_text(json.dumps({'port': 1, 'token': 't', 'pid': 99999999}))
        self.assertIsNone(copilot_bridge.read_info(gone))
        junk = Path(self.tmp.name) / 'junk.json'
        junk.write_text('not json')
        self.assertIsNone(copilot_bridge.read_info(junk))

    def test_only_loopback_is_contacted_and_proxies_are_ignored(self):
        b, info = self.bridge(lambda body: (200, {'text': '{"a": 1}'}))
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:9', 'http_proxy': 'http://127.0.0.1:9'}):
            self.assertEqual(copilot_bridge.CopilotLLM(info=info).ask_json('s', 'u'), {'a': 1})


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = patch.object(ai_setup, 'SETTINGS_FILE', Path(self.tmp.name) / 'ai.json'); p.start(); self.addCleanup(p.stop)
        local_llm._CACHE.clear(); self.addCleanup(local_llm._CACHE.clear)

    def test_default_provider_is_local_and_existing_behaviour_is_unchanged(self):
        self.assertEqual(ai_setup.effective({})['provider'], 'local')
        self.assertFalse(ai_setup.feature_on({}, 'tester_note'))
        ai_setup.save({'enabled': True, 'model_path': '/m/x.gguf'})
        self.assertTrue(ai_setup.feature_on({}, 'tester_note'))
        self.assertIsNotNone(local_llm.get_llm({}))
        self.assertNotIsInstance(local_llm.get_llm({}), copilot_bridge.CopilotLLM)

    def test_copilot_provider_needs_no_model_file(self):
        ai_setup.save({'enabled': True, 'provider': 'copilot'})
        self.assertTrue(ai_setup.feature_on({}, 'tester_note'))
        self.assertTrue(ai_setup.feature_on({}, 'failure_hint'))
        self.assertIsInstance(local_llm.get_llm({}), copilot_bridge.CopilotLLM)

    def test_invalid_provider_is_ignored(self):
        ai_setup.save({'provider': 'evil'})
        self.assertEqual(ai_setup.effective({})['provider'], 'local')

    def test_api_switches_provider_and_enables_without_a_model_file(self):
        code, status = ai_api.handle_post('/api/ai/settings', {'provider': 'copilot', 'enabled': True})
        self.assertEqual(code, 200)
        self.assertEqual((status['provider'], status['enabled']), ('copilot', True))
        self.assertIn('copilot', status)
        code, _ = ai_api.handle_post('/api/ai/settings', {'provider': 'local', 'enabled': True})
        self.assertEqual(code, 400)                         # local still requires a model file
        code, _ = ai_api.handle_post('/api/ai/settings', {'provider': 'nonsense'})
        self.assertEqual(code, 200)
        self.assertEqual(ai_setup.saved()['provider'], 'copilot')

    def test_note_and_hint_helpers_work_through_the_bridge(self):
        b = FakeBridge(lambda body: (200, {'text': json.dumps({'note': 'Check the consent screen and a full transfer.'})}))
        self.addCleanup(b.close)
        info = b.info_file(self.tmp.name)
        ai_setup.save({'enabled': True, 'provider': 'copilot'})
        with patch.object(copilot_bridge, 'INFO', info):
            llm = local_llm.get_llm({})
            self.assertEqual(local_llm.tester_note(llm, 'TRANSACT-1: Fix consent', 'Sends the right account id.'),
                             'Check the consent screen and a full transfer.')


class InstallerTests(unittest.TestCase):
    def test_install_copies_the_extension_and_replaces_old_versions(self):
        with tempfile.TemporaryDirectory() as home:
            exts = Path(home) / '.vscode' / 'extensions'
            exts.mkdir(parents=True)
            old = exts / 'app-builder.copilot-bridge-0.0.1'; old.mkdir()
            other = exts / 'someone.else-1.0.0'; other.mkdir()
            with patch.dict(os.environ, {'APP_BUILDER_VSCODE_EXTENSIONS': str(exts)}):
                dest = vscode_ext.install()
                self.assertTrue((dest / 'extension.js').is_file() and (dest / 'lib' / 'bridge.js').is_file())
                self.assertFalse((dest / 'test').exists())
                self.assertFalse(old.exists())
                self.assertTrue(other.exists())
                self.assertIn('已安装', vscode_ext.run('status'))
                self.assertEqual(vscode_ext.uninstall(), [dest])
                self.assertTrue(other.exists())
                self.assertEqual(vscode_ext.run('uninstall'), '没有安装')

    def test_install_explains_when_vscode_was_never_installed(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {'APP_BUILDER_VSCODE_EXTENSIONS': str(Path(home) / 'none' / 'extensions')}):
                with self.assertRaisesRegex(RuntimeError, 'VS Code'):
                    vscode_ext.install()

    def test_package_json_matches_what_the_installer_expects(self):
        m = vscode_ext.meta()
        self.assertEqual((m['publisher'], m['name']), ('app-builder', 'copilot-bridge'))
        self.assertTrue((vscode_ext.SOURCE / m['main']).is_file())


if __name__ == '__main__':
    unittest.main()
