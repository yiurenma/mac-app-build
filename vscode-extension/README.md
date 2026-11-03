# APP Builder Copilot Bridge

A tiny VS Code extension. It lets APP Builder (the local console) ask **VS Code's Copilot** for the
two optional one-sentence AI helpers (tester note on PR comments, "Likely cause / Try" in failure
emails). No build step, no npm dependencies.

## Install (on the Mac)

```bash
./app-build vscode-bridge install     # copies this folder into ~/.vscode/extensions
```

Then **quit and reopen VS Code**, sign in to Copilot, and in APP Builder go to
Settings → AI → choose "VS Code 里的 Copilot" and turn it on. The first call makes VS Code ask
for permission once; click Allow.

`./app-build vscode-bridge status` shows whether the extension is installed and reachable;
`./app-build vscode-bridge uninstall` removes it.

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
