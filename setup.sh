#!/usr/bin/env bash
set -euo pipefail
demo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$demo_root"
demo_python="${DEMO_PYTHON:-python3}"
if [[ ! -x .venv/bin/python ]]; then
  "$demo_python" -c 'import sys; assert sys.version_info >= (3,12), "需要 Python 3.12 或更高版本"'
  "$demo_python" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -c 'import shutil; assert shutil.which("node") and shutil.which("npm"), "需要 Node.js 22.18+ 和 npm"'
(cd frontend && npm ci && npm run build)
echo '应用环境已构建。运行 ./start.sh，然后打开 http://127.0.0.1:8766。'
