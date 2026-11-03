"""End-to-end check in a REAL VS Code extension host (code-server ships VS Code's core).

Usage: python3 vscode-extension/test/e2e/run.py /path/to/node_modules/.bin/code-server
Starts code-server with the bridge extension and a fake language model, opens it once in
headless Chromium so the extension host starts, then drives the bridge with the project's
own Python client.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from appbuilder import copilot_bridge  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f'  {detail}'), flush=True)
    if not cond:
        failures.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def serve_forever(code_server, work):
    """Stand-in for `open -g -j -a "Visual Studio Code"`: start VS Code's core and a client, stay up."""
    work = Path(work)
    port = free_port()
    env = {**os.environ, 'HOME': str(work)}
    server = subprocess.Popen([code_server, '--auth', 'none', '--bind-addr', f'127.0.0.1:{port}',
                               '--user-data-dir', str(work / 'user'), '--extensions-dir', str(work / 'ext'),
                               '--disable-telemetry', '--disable-update-check', str(work)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).read()
            break
        except OSError:
            time.sleep(1)
    from playwright.sync_api import sync_playwright
    sys.path.insert(0, str(ROOT / 'tests'))
    from chromium_path import CHROMIUM
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=['--no-sandbox'])
        browser.new_page().goto(f'http://127.0.0.1:{port}/?folder={work}')
        time.sleep(3600)
    server.terminate()


def main(code_server):
    work = Path(tempfile.mkdtemp(prefix='bridge-e2e-'))
    ext_dir, user_dir = work / 'ext', work / 'user'
    (user_dir / 'User').mkdir(parents=True)
    ext_dir.mkdir()
    shutil.copytree(ROOT / 'vscode-extension', ext_dir / 'app-builder.copilot-bridge-0.1.0',
                    ignore=shutil.ignore_patterns('test', 'node_modules'))
    shutil.copytree(HERE / 'fake-model', ext_dir / 'app-builder-test.fake-model-0.0.1')
    (user_dir / 'User' / 'settings.json').write_text(json.dumps({
        'appBuilderBridge.vendor': 'appbuilder-fake', 'security.workspace.trust.enabled': False,
        'workbench.startupEditor': 'none', 'telemetry.telemetryLevel': 'off'}))
    info = work / 'bridge.json'
    port = free_port()
    env = {**os.environ, 'APP_BUILDER_BRIDGE_INFO': str(info), 'HOME': str(work)}
    log = (work / 'server.log').open('w')
    server = subprocess.Popen([code_server, '--auth', 'none', '--bind-addr', f'127.0.0.1:{port}',
                               '--user-data-dir', str(user_dir), '--extensions-dir', str(ext_dir),
                               '--disable-telemetry', '--disable-update-check', str(work)],
                              env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).read()
                break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit('code-server did not start:\n' + (work / 'server.log').read_text()[-2000:])
        from playwright.sync_api import sync_playwright
        sys.path.insert(0, str(ROOT / 'tests'))
        from chromium_path import CHROMIUM
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=['--no-sandbox'])
            page = browser.new_page()
            page.goto(f'http://127.0.0.1:{port}/?folder={work}')
            for _ in range(90):                      # extension host starts after the workbench loads
                if info.exists():
                    break
                time.sleep(1)
            check('extension activated and wrote its info file', info.exists(), (work / 'server.log').read_text()[-1500:])
            if info.exists():
                mode = info.stat().st_mode & 0o777
                check('info file is private (0600)', mode == 0o600, oct(mode))
                llm = copilot_bridge.CopilotLLM(info=info, timeout=60)
                check('health check passes', copilot_bridge.check(info)['ok'])
                check('available() is true', llm.available())
                out = llm.ask_json('Output only JSON.', 'hello bridge')
                check('python client gets a streamed answer through VS Code', bool(out) and out.get('note', '').startswith('FAKE:'), out)
                check('system prompt and user text both reach the model', bool(out) and 'Output only JSON.' in out['echo'] and 'hello bridge' in out['echo'], out)
                check('plain-text answer is not treated as JSON', llm.ask_json('s', 'NOJSON please') is None)
                check('model failure returns None, not an exception', llm.ask_json('s', 'EXPLODE') is None)
                check('still healthy after a failure', llm.ask_json('s', 'again') is not None)
                # Security: wrong token and browser-style Origin are refused.
                data = json.loads(info.read_text())
                for label, headers in (('wrong token', {'Authorization': 'Bearer nope'}),
                                       ('browser Origin', {'Authorization': 'Bearer ' + data['token'], 'Origin': 'http://evil.example'})):
                    req = urllib.request.Request(f"http://127.0.0.1:{data['port']}/health", headers=headers)
                    try:
                        urllib.request.urlopen(req, timeout=5)
                        check(label + ' rejected', False, 'got 200')
                    except urllib.error.HTTPError as exc:
                        check(label + ' rejected', exc.code == 401, exc.code)
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        try:
            os.killpg(server.pid, 9)          # the extension host is a child process: end the whole group
        except ProcessLookupError:
            pass
        time.sleep(1)
        log.close()
    # code-server kills its extension host hard, so the file may remain; the client must then ignore it.
    check('after VS Code exits the client no longer sees a bridge', not copilot_bridge.check(info)['ok'])
    # Phase 2: VS Code is closed; the AI asks, the launcher opens it, the question is answered.
    launcher = subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve()), '--launch', code_server, str(work)])
    import shlex
    launch_env = {'APP_BUILDER_BRIDGE_INFO': str(info), 'APP_BUILDER_VSCODE_LAUNCH': ' '.join(
        shlex.quote(x) for x in [sys.executable, str(Path(__file__).resolve()), '--launch', code_server, str(work)]),
        'APP_BUILDER_VSCODE_WAIT': '120', 'HOME': str(work)}
    copilot_bridge.INFO = info
    copilot_bridge._LAST_FAILED[0] = 0.0
    old = {k: os.environ.get(k) for k in launch_env}
    os.environ.update(launch_env)
    copilot_bridge.START_WAIT = 120
    started = time.time()
    try:
        answer = copilot_bridge.CopilotLLM(auto_start=True, timeout=90).ask_json('Output only JSON.', 'auto start please')
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check('AI call with VS Code closed opens it by itself and gets an answer', bool(answer) and answer.get('note', '').startswith('FAKE:'), answer)
    print(f'     (cold start + answer took {time.time() - started:.0f}s)')
    import signal
    for line in subprocess.run(['pgrep', '-f', 'run.py --launch'], capture_output=True, text=True).stdout.split():
        try:
            os.kill(int(line), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    subprocess.run(['pkill', '-9', '-f', str(work)], capture_output=True)
    shutil.rmtree(work, ignore_errors=True)
    print('RESULT: ' + ('FAILED ' + ', '.join(failures) if failures else 'all passed'))
    return 1 if failures else 0


if __name__ == '__main__':
    if sys.argv[1] == '--launch':
        serve_forever(sys.argv[2], sys.argv[3])
    else:
        sys.exit(main(sys.argv[1]))
