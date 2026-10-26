"""Offline local LLM helper (llama.cpp GGUF). No network, no external API.

The model is only an advisor: callers must validate its output against facts they already
hold. If the model file or the llama-cpp-python package is missing, every function returns
None and the caller falls back to plain rules.
"""
from __future__ import annotations
import json
import re
import threading
from pathlib import Path
from .telemetry import action, safe_text

_LOCK = threading.Lock()
_CACHE = {}


class LocalLLM:
    def __init__(self, model_path, *, n_ctx=4096, n_threads=None, n_gpu_layers=-1, max_tokens=256):
        self.model_path = Path(str(model_path)).expanduser() if model_path else None
        self.n_ctx = int(n_ctx)
        self.n_threads = n_threads
        self.n_gpu_layers = int(n_gpu_layers)
        self.max_tokens = int(max_tokens)
        self._model = None
        self._failed = None

    def available(self):
        if self._model is not None:
            return True
        if self._failed:
            return False
        if not self.model_path or not self.model_path.is_file():
            self._failed = '未找到本地模型文件'
            action('AI', f'{self._failed}；跳过 AI，使用纯规则')
            return False
        try:
            from llama_cpp import Llama
        except Exception as exc:
            self._failed = 'llama-cpp-python 未安装'
            action('AI', f'{self._failed}（{type(exc).__name__}）；跳过 AI，使用纯规则')
            return False
        try:
            with _LOCK:
                kwargs = dict(model_path=str(self.model_path), n_ctx=self.n_ctx,
                              n_gpu_layers=self.n_gpu_layers, verbose=False)
                if self.n_threads:
                    kwargs['n_threads'] = int(self.n_threads)
                self._model = Llama(**kwargs)
            action('AI', f'本地模型已加载: {self.model_path.name}')
            return True
        except Exception as exc:
            self._failed = '本地模型加载失败'
            action('AI', safe_text(f'{self._failed}: {exc}；使用纯规则'))
            return False

    def ask_json(self, system, user):
        """Return a parsed JSON object or None. Temperature 0; any problem returns None."""
        if not self.available():
            return None
        try:
            if 'qwen3' in self.model_path.name.lower():
                user += ' /no_think'                     # Qwen3: answer directly, no long "thinking"
            with _LOCK:
                out = self._model.create_chat_completion(
                    messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                    temperature=0.0, max_tokens=self.max_tokens,
                    response_format={'type': 'json_object'})
            text = out['choices'][0]['message']['content']
            match = re.search(r'\{.*\}', text, re.S)
            data = json.loads(match.group(0) if match else text)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            action('AI', safe_text(f'模型调用失败: {type(exc).__name__}; 使用纯规则'))
            return None


def get_llm(config):
    """Shared instance per model path; returns None when AI is not configured.
    Settings saved on the page (ai_setup) override config.yaml."""
    from .ai_setup import effective
    ai = effective(config)
    if not ai.get('enabled'):
        return None
    path = ai.get('model_path') or ''
    if not path:
        return None
    key = (str(path), ai.get('n_ctx', 4096))
    if key not in _CACHE:
        _CACHE[key] = LocalLLM(path, n_ctx=ai.get('n_ctx', 4096), n_threads=ai.get('n_threads'),
                               n_gpu_layers=ai.get('n_gpu_layers', -1), max_tokens=ai.get('max_tokens', 256))
    return _CACHE[key]


def pick_primary_ticket(llm, candidates, commit_text):
    """Ask the model to choose ONE key among candidates. Output is validated: a key the
    model invents (not in candidates) is rejected and None is returned."""
    if not llm or len(candidates) < 2:
        return None
    data = llm.ask_json(
        '你是代码提交分析器。给定一条提交说明和若干 Jira 编号，只选出这次改动主要对应的一个编号。'
        '只能从给定列表中选择。只输出 JSON：{"key": "<编号>"}；无法判断则 {"key": null}。',
        f'候选编号: {json.dumps(sorted(candidates))}\n提交说明:\n{commit_text[:3000]}')
    key = (data or {}).get('key')
    return key if isinstance(key, str) and key in candidates else None


def tester_summary(llm, subject, body=''):
    """One or two plain sentences for testers. Falls back to the commit subject."""
    fallback = subject.strip()[:300]
    if not llm:
        return fallback
    data = llm.ask_json(
        'Rewrite a commit message as one or two plain English sentences for testers: what changed '
        'and what to verify. Do not add anything that is not in the message. Output only JSON: '
        '{"summary": "<text>"}.',
        f'Commit subject: {subject}\nCommit body:\n{body[:2000]}')
    text = (data or {}).get('summary')
    if isinstance(text, str) and 3 <= len(text.strip()) <= 400:
        return text.strip()
    return fallback


# ---- checks shared by everything the model writes for other people --------------------------
_KEY = re.compile(r'\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b')
_CJK = re.compile(r'[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]')


def clean_sentence(text, source, limit=220):
    """The model's text if it is safe to show the team, else None: English only, short,
    no links, no markup, and no ticket number that the source text does not contain."""
    if not isinstance(text, str):
        return None
    text = ' '.join(text.split()).strip(' "\'')
    if not (8 <= len(text) <= limit) or _CJK.search(text) or re.search(r'https?://|www\.', text):
        return None
    if re.search(r'[{}\[\]|<>*_^~#]', text):
        return None
    if set(_KEY.findall(text)) - set(_KEY.findall(source or '')):
        return None
    return text


def tester_note(llm, title, body=''):
    """One sentence for testers: what changed and what to check. None when unsure."""
    if not llm or not (title or '').strip():
        return None
    source = f'{title}\n{(body or "")[:2500]}'
    data = llm.ask_json(
        'You help software testers. From a pull request, write ONE plain English sentence (max 30 '
        'words) saying what changed and what a tester should check. Use only facts in the text. '
        'No links, no ticket numbers, no markdown. Output only JSON: {"note": "<sentence>"}.',
        f'Title: {title}\nDescription:\n{(body or "")[:2500]}')
    return clean_sentence((data or {}).get('note'), source)


def failure_hint(llm, step, reason, log_lines):
    """{'cause', 'fix'} in plain English for a failed build, or None. Only English input is
    given to the model, and only checked English comes back."""
    if not llm:
        return None
    source = '\n'.join([step or '', reason or '', *log_lines])
    if len(source.strip()) < 10:
        return None
    data = llm.ask_json(
        'You help an iOS build engineer. A Jenkins-based build failed. From the step, reason and '
        'log lines, give the most likely cause and one concrete next action, each in ONE short '
        'plain English sentence. Use only what the text supports; if unsure say so. No links, no '
        'markdown. Output only JSON: {"cause": "<sentence>", "fix": "<sentence>"}.',
        f'Step: {step}\nReason: {reason}\nLog lines:\n' + '\n'.join(log_lines[-12:])[:3000])
    cause = clean_sentence((data or {}).get('cause'), source, 200)
    fix = clean_sentence((data or {}).get('fix'), source, 200)
    return {'cause': cause, 'fix': fix} if cause and fix else None
