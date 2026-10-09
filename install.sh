#!/bin/sh
# Enterprise-friendly installer. Never silently fall back to public package repositories.
set -eu
cd "$(dirname "$0")"

usage() {
  cat <<'HELP'
用法: ./install.sh

先设置企业 Python 包仓库（示例仅为占位符）:
  export PIP_INDEX_URL='https://nexus.company.example/repository/pypi-group/simple/'
  # 如企业 CA 不在系统信任链中，按 IT 指引设置：
  # export PIP_CERT='/path/to/company-ca.pem'

浏览器选择在 config.yaml -> jenkins.browser_channel 中配置:
  chrome     已安装的企业 Chrome，跳过 Chromium 下载（推荐）
  msedge     已安装的企业 Edge，跳过 Chromium 下载
  chromium   Playwright Chromium，需先配置 PLAYWRIGHT_DOWNLOAD_HOST
             或 PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST 指向内部镜像

可选：APP_BUILDER_SKIP_BROWSER_INSTALL=1（浏览器已由 IT 预装或缓存）
公开 PyPI 和公开 Playwright 下载不会自动启用。
HELP
}
if [ "${1:-}" = '--help' ] || [ "${1:-}" = '-h' ]; then usage; exit 0; fi
if [ "$#" -ne 0 ]; then usage; exit 2; fi

command -v python3 >/dev/null 2>&1 || {
  echo '缺少 python3。请通过公司批准的软件分发渠道安装 Python 3.11+。' >&2
  exit 1
}
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit('需要 Python >= 3.11，请使用公司批准的安装渠道。')
PY

# Corporate pip config can be set by PIP_INDEX_URL, user/global pip.conf, or PIP_CONFIG_FILE.
if [ -z "${PIP_INDEX_URL:-}" ]; then
  index_config="$(python3 -m pip config get global.index-url 2>/dev/null || true)"
  if [ -z "$index_config" ]; then
    index_config="$(python3 -m pip config get install.index-url 2>/dev/null || true)"
  fi
  if [ -z "$index_config" ]; then
    echo '未检测到公司 Python 包仓库。为避免访问公网 PyPI，安装已停止。' >&2
    usage >&2
    exit 1
  fi
  case "$index_config" in
    *pypi.org*|*pythonhosted.org*)
      echo '当前 pip index 配置指向公网 PyPI，请先配置公司 Nexus。' >&2
      exit 1
      ;;
  esac
else
  case "$PIP_INDEX_URL" in
    *pypi.org*|*pythonhosted.org*)
      echo 'PIP_INDEX_URL 指向公网 PyPI；请改成公司 Nexus。' >&2
      exit 1
      ;;
  esac
fi

# Never silently use a secondary index: package resolution across indexes can
# fall back to public registries and increase dependency-confusion risk.
extra_index="${PIP_EXTRA_INDEX_URL:-}"
if [ -z "$extra_index" ]; then
  extra_index="$(python3 -m pip config get global.extra-index-url 2>/dev/null || true)"
fi
if [ -z "$extra_index" ]; then
  extra_index="$(python3 -m pip config get install.extra-index-url 2>/dev/null || true)"
fi
if [ -n "$extra_index" ]; then
  echo '发现额外的 pip package index。为避免依赖混淆和意外访问外部仓库，请使用公司 Nexus group 单一索引，并清除 PIP_EXTRA_INDEX_URL / pip.conf extra-index-url。' >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip --disable-pip-version-check install -r requirements.txt

# Read channel without adding external tool dependencies; PyYAML is now in the venv.
browser_channel="$(.venv/bin/python - <<'PY'
from appbuilder.config import load_config
print(load_config()['jenkins'].get('browser_channel', 'chromium'))
PY
)"
case "$browser_channel" in
  chrome)
    echo '浏览器：使用公司已经安装的 Google Chrome，不下载 Playwright Chromium。'
    ;;
  msedge)
    echo '浏览器：使用公司已经安装的 Microsoft Edge，不下载 Playwright Chromium。'
    ;;
  chromium)
    if [ "${APP_BUILDER_SKIP_BROWSER_INSTALL:-0}" = '1' ]; then
      echo '浏览器：跳过下载，请确认 Playwright Chromium 已由 IT 安装/缓存。'
    else
      if [ -z "${PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST:-}" ] && [ -z "${PLAYWRIGHT_DOWNLOAD_HOST:-}" ]; then
        echo '未设置企业 Chromium 下载镜像。请设置 PLAYWRIGHT_DOWNLOAD_HOST / PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST，或在 config.yaml 选 browser_channel: chrome。' >&2
        exit 1
      fi
      .venv/bin/python -m playwright install chromium
    fi
    ;;
  *)
    echo "不支持 jenkins.browser_channel=$browser_channel (允许 chromium/chrome/msedge)" >&2
    exit 1
    ;;
esac
printf '\n安装完成。后续执行：\n  ./app-build doctor\n  ./app-build login --site plugin\n  ./app-build login --site gmx\n'
