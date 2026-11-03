'use strict';
// A stand-in for Copilot so the bridge can be exercised in a real VS Code extension host.
const vscode = require('vscode');

function lastUserText(messages) {
  const last = messages[messages.length - 1];
  return last.content.map((p) => (p && p.value) || '').join('');
}

exports.activate = (context) => {
  context.subscriptions.push(vscode.lm.registerLanguageModelChatProvider('appbuilder-fake', {
    provideLanguageModelChatInformation() {
      return [{ id: 'fake-1', name: 'Fake', family: 'fake', version: '1', maxInputTokens: 8000, maxOutputTokens: 1000, capabilities: {} }];
    },
    async provideLanguageModelChatResponse(model, messages, options, progress) {
      const text = lastUserText(messages);
      if (text.includes('EXPLODE')) throw new Error('fake model failure');
      if (text.includes('NOJSON')) { progress.report(new vscode.LanguageModelTextPart('sorry, plain text only')); return; }
      // Echo the prompt back inside JSON, in two chunks to exercise streaming.
      const answer = JSON.stringify({ note: 'FAKE:' + text.length, echo: text.slice(0, 200) });
      progress.report(new vscode.LanguageModelTextPart(answer.slice(0, 10)));
      progress.report(new vscode.LanguageModelTextPart(answer.slice(10)));
    },
    async provideTokenCount(model, text) { return String(text).length / 4; },
  }));
};
