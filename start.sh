#!/usr/bin/env bash
set -euo pipefail
demo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$demo_root"
demo_port="${DEMO_PORT:-8766}"
mkdir -p .local-data
if [[ ! -x .venv/bin/python || ! -f frontend/dist/index.html ]]; then
  echo '运行环境或界面尚未构建，请先运行 ./setup.sh。'
  exit 1
fi
if .venv/bin/python scripts/service_identity.py "$demo_port"; then
  echo "图序社区版已运行：http://127.0.0.1:$demo_port"
  exit 0
fi
if .venv/bin/python -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1",int(sys.argv[1])))==0 else 1)' "$demo_port"; then
  echo "端口 $demo_port 已占用，请通过 DEMO_PORT 更换端口。"
  exit 1
fi
if [[ -f .local-data/server.pid ]]; then
  demo_old_pid="$(cat .local-data/server.pid)"
  demo_old_command="$(ps -p "$demo_old_pid" -o command= 2>/dev/null || true)"
  if [[ "$demo_old_command" == *'uvicorn backend.app.main:app'* ]]; then
    echo '本应用进程仍在启动或停止收尾，请查看日志后再试。'
    exit 1
  fi
fi
demo_pid="$(.venv/bin/python scripts/launch_service.py --cwd "$demo_root" --log "$demo_root/.local-data/server.log" --pid "$demo_root/.local-data/server.pid" -- "$demo_root/.venv/bin/python" -m uvicorn backend.app.main:app --host 127.0.0.1 --port "$demo_port")"
for ((demo_wait=0; demo_wait<40; demo_wait++)); do
  if .venv/bin/python scripts/service_identity.py "$demo_port"; then
    echo "图序社区版已启动：http://127.0.0.1:$demo_port"
    exit 0
  fi
  if ! kill -0 "$demo_pid" 2>/dev/null; then
    echo '启动失败，请查看 .local-data/server.log。'
    exit 1
  fi
  sleep 0.5
done
echo '启动仍未就绪，请查看 .local-data/server.log。'
exit 1
