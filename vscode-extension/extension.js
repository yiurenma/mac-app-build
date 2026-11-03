'use strict';
const vscode = require('vscode');
const { createBridge, BridgeError } = require('./lib/bridge');

let bridge = null;
let status = null;

function settings() { return vscode.workspace.getConfiguration('appBuilderBridge'); }

async function askModel({ system, user }) {
  const cfg = settings();
  const selector = { vendor: cfg.get('vendor') || 'copilot' };
  if (cfg.get('family')) selector.family = cfg.get('family');
  const models = await vscode.lm.selectChatModels(selector);
  if (!models.length) throw new BridgeError(503, 'no language model available: sign in to GitHub Copilot in VS Code');
  const prompt = system ? `${system}\n\n---\n\n${user}` : user;
  const cts = new vscode.CancellationTokenSource();
  const timer = setTimeout(() => cts.cancel(), 80000);
  try {
    const response = await models[0].sendRequest(
      [vscode.LanguageModelChatMessage.User(prompt)],
      { justification: 'APP Builder writes a short tester note or failure hint.' },
      cts.token);
    let text = '';
    for await (const part of response.text) text += part;
    return text;
  } catch (err) {
    if (err instanceof BridgeError) throw err;
    if (err instanceof vscode.LanguageModelError) {
      if (err.code === vscode.LanguageModelError.NoPermissions.name) throw new BridgeError(403, 'permission not granted: allow APP Builder Copilot Bridge in the VS Code prompt');
      if (err.code === vscode.LanguageModelError.Blocked.name) throw new BridgeError(429, 'request blocked or rate limited');
      if (err.code === vscode.LanguageModelError.NotFound.name) throw new BridgeError(503, 'model not found');
      throw new BridgeError(502, 'model error: ' + err.code);
    }
    throw err;
  } finally {
    clearTimeout(timer);
    cts.dispose();
  }
}

function show(text) { if (status) { status.text = text; status.show(); } }

async function startBridge(context) {
  if (bridge) await bridge.stop();
  bridge = null;
  if (!settings().get('enabled')) { show('$(circle-slash) App Builder bridge off'); return; }
  const out = vscode.window.createOutputChannel('APP Builder Bridge');
  const version = (context.extension && context.extension.packageJSON && context.extension.packageJSON.version) || '0.1.0';
  bridge = createBridge({ ask: askModel, version, log: (m) => out.appendLine(m) });
  try {
    const port = await bridge.start();
    show('$(plug) App Builder bridge :' + port);
  } catch (err) {
    bridge = null;
    show('$(error) App Builder bridge failed');
    out.appendLine('start failed: ' + err.message);
  }
}

async function activate(context) {
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  status.command = 'appBuilderBridge.status';
  context.subscriptions.push(status);
  context.subscriptions.push(
    vscode.commands.registerCommand('appBuilderBridge.status', async () => {
      const models = await vscode.lm.selectChatModels({ vendor: settings().get('vendor') || 'copilot' });
      vscode.window.showInformationMessage(bridge
        ? `APP Builder bridge listening on 127.0.0.1:${bridge.port}; ${models.length} model(s) available.`
        : 'APP Builder bridge is not running.');
    }),
    vscode.commands.registerCommand('appBuilderBridge.restart', () => startBridge(context)),
    vscode.workspace.onDidChangeConfiguration((e) => { if (e.affectsConfiguration('appBuilderBridge')) startBridge(context); }),
  );
  await startBridge(context);
  return { _test: { askModel, get port() { return bridge && bridge.port; }, get token() { return bridge && bridge.token; } } };
}

async function deactivate() { if (bridge) await bridge.stop(); bridge = null; }

module.exports = { activate, deactivate };
