# APP Builder Copilot Bridge

A tiny VS Code extension. It lets APP Builder (the local console) ask **VS Code's Copilot** for the
two optional one-sentence AI helpers (tester note on PR comments, "Likely cause / Try" in failure
emails). No build step, no npm dependencies.

## Use it

In APP Builder: **Settings → AI → "用哪种 AI" → VS Code 里的 Copilot**. That installs this extension
into `~/.vscode/extensions` and switches the AI on. Then quit and reopen VS Code, sign in to Copilot,
and click Allow the first time VS Code asks.

Then paste a PR link under "用一个真实 PR 试试" and click the button to see what is sent and what the AI writes.

Terminal alternative: `./app-build vscode-bridge install | status | uninstall`.

## How it works

- On start-up the extension listens on `127.0.0.1` (random port) and writes
  `~/.app-builder/vscode-bridge.json` (mode 0600: port, one-time token, pid).
- `POST /ask {system, user}` → `vscode.lm.selectChatModels({vendor:'copilot'})` → answer text.
- Every request needs `Authorization: Bearer <token>` and a loopback `Host`; any request with an
  `Origin` header (i.e. from a web page) is refused. Questions are handled one at a time.
- VS Code must be open and signed in. If it is not, APP Builder silently falls back to its plain rules.

Settings: `appBuilderBridge.enabled`, `appBuilderBridge.vendor` (default `copilot`), `appBuilderBridge.family`.

## Tests

```bash
node --test vscode-extension/test/bridge.test.js          # protocol, auth, limits (no VS Code)
python3 vscode-extension/test/e2e/run.py <code-server>    # real VS Code extension host + fake model
```
