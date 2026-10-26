from __future__ import annotations
import os
from urllib.parse import urlsplit
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".appbuilder"


def load_config():
    path = Path(os.environ.get("APP_BUILDER_CONFIG", ROOT / "config.yaml"))
    with path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError("config.yaml 格式错误")
    return config


def token_or_error(config):
    key = config.get("github", {}).get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(key, "").strip()
    if not token:
        from .auth import read_github_token
        token = (read_github_token() or "").strip()
    if not token:
        repo = config.get('github', {}).get('plugin_url', '')
        parsed = urlsplit(repo)
        enterprise = f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme == 'https' and parsed.netloc else '你的企业 GitHub'
        raise RuntimeError(
            f"未设置 {key}。请在企业 GitHub 的 Settings → Developer settings → "
            f"Personal access tokens 中生成 Token：{enterprise}/settings/tokens\n"
            "根据企业 GitHub 版本选择支持的 PAT 类型，并申请 Plugin、GMX 仓库所需读写权限；"
            "企业可能要求 SSO 或管理员审批。\n"
            f"当前终端设置：export {key}='TOKEN值'\n"
            "macOS 推荐使用 Keychain/企业密码管理器，不要明文写入脚本。"
        )
    return token


def parallel_limit(config):
    """Manual, scheduled and resumed pipelines share at most ten slots."""
    value = config.get('build', {}).get('max_parallel', 10)
    if isinstance(value, bool):
        raise ValueError('max_parallel 必须为 1 到 10 的整数')
    try:
        limit = int(value)
    except (ValueError, TypeError):
        raise ValueError('max_parallel 必须为 1 到 10 的整数') from None
    if str(value).strip() != str(limit) or limit < 1:
        raise ValueError('max_parallel 必须为 1 到 10 的整数')
    return min(limit, 10)
