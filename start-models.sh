#!/usr/bin/env bash
set -euo pipefail
# External models are installed and started separately; never clear shared queues.
if curl --noproxy '*' --silent --fail --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null; then
  echo 'Ollama服务可连接；请在界面选择已安装的文章理解模型。'
else
  echo 'Ollama尚不可连接。安装官方软件后运行 ollama serve，并按文档下载模型。'
fi
if curl --noproxy '*' --silent --fail --max-time 3 http://127.0.0.1:8188/system_stats >/dev/null; then
  echo 'ComfyUI服务可连接；是否可生图仍需配置模型并检查节点。'
else
  echo '本地生图可选。请自行启动ComfyUI，或在界面配置自己的云生图API。'
fi
echo '此脚本只检查服务；安装和启动方法见 docs/models.md。'
