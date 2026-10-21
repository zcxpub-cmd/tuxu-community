#!/usr/bin/env bash
set -euo pipefail
demo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$demo_root"
if [[ -f .local-data/server.pid ]]; then
  demo_pid="$(cat .local-data/server.pid)"
  demo_command="$(ps -p "$demo_pid" -o command= 2>/dev/null || true)"
  if [[ -d "/proc/$demo_pid" ]]; then
    demo_cwd="$(readlink "/proc/$demo_pid/cwd" 2>/dev/null || true)"
  else
    demo_cwd="$(lsof -a -p "$demo_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
  fi
  if [[ "$demo_command" == *'uvicorn backend.app.main:app'* && "$demo_cwd" == "$demo_root" ]]; then
    kill -TERM "$demo_pid"
    for ((demo_wait=0; demo_wait<40; demo_wait++)); do
      if ! kill -0 "$demo_pid" 2>/dev/null || [[ "$(ps -p "$demo_pid" -o stat= 2>/dev/null || true)" == *Z* ]]; then
        echo '图序应用已停止；模型服务与任务记录保留。'
        exit 0
      fi
      sleep 0.25
    done
    echo '已请求停止，当前模型任务仍在收尾。请等待其结束后再启动。'
    exit 1
  else
    echo '记录的应用进程未运行。'
  fi
else
  echo '没有本应用的启动记录。'
fi
