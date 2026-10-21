# 安装与运行

## 环境

- Python 3.12 或更高版本，支持创建 `venv`。
- Node.js 22.18 或更高版本和 npm；前端检查直接运行 TypeScript 源码。
- 支持 macOS/Linux，启动脚本使用 Bash。Windows 请使用 WSL 的 Linux 环境；包含文件锁的后台不支持原生 Windows。
- 全文分析：运行本地 Ollama，并安装可按 JSON 协议输出的模型。在界面的“文章理解模型”中填写与你安装一致的名称。
- 图片生成：自行配置一种生图路线即可，本地 ComfyUI 不是使用云 API 的前提。

仓库不附带大模型权重，也不自动购买 API 服务。请先确认模型与素材的使用权。

## 安装依赖

在获取的仓库目录中执行：

```bash
./setup.sh
```

脚本创建 Python 虚拟环境并安装锁定依赖，随后构建前端。若系统 `python3` 不是需要的版本，可指定已安装的 Python：

```bash
DEMO_PYTHON=python3.12 ./setup.sh
```

不要以管理员身份运行应用。依赖安装需要访问相应软件包源；失败时先检查终端中的失败步骤，再重新执行该步骤。

## 启动、停止与数据

```bash
./start.sh
```

浏览器打开 [http://127.0.0.1:8766](http://127.0.0.1:8766)。服务默认仅监听回环地址，供本机使用。

```bash
./stop.sh
```

用户数据默认存放于独立的 `TuxuCommunity` 数据目录，包含原稿副本、任务、生成图片、导出文件和本机配置。运行日志及进程标识位于仓库内的 `.local-data/`，不应提交到版本库。

如需另设目录，使用同一环境变量启动服务和 CLI：

```bash
export TUXU_DATA_DIR="$HOME/.local/share/tuxu-community-data"
./start.sh
```

更换目录不会自动迁移旧任务。备份或迁移前先停止服务，保留完整数据目录；不要把已有应用的数据库作为空白测试环境。

默认端口被占用时，可在启动前设置 `DEMO_PORT`。前端开发代理与 CLI 地址也需同步，例如：

```bash
DEMO_PORT=8876 ./start.sh
```

环境变量需自行 `export` 或在命令前设置。应用不会自动读取 `.env`；仅复制变量参考文件不会生效。手动启动也可直接指定端口。

## 手动启动

使用已安装的 Python 创建环境，并安装后端、构建前端：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8766
```

Windows 用户应在 WSL 的 Linux 终端中执行上述步骤；本版本不承诺原生 Windows 兼容。WSL 中的模型服务地址和文件路径也需按实际运行环境配置。

## 启动后检查

1. 页面显示“图序社区版 v0.1.0”。
2. 刷新模型状态，确认文章理解模型可用；服务启动成功不代表已经安装模型。
3. 如使用云 API，展开配置，保存自己的地址、模型、密钥，测试已保存配置。
4. 首次先用界面中的自制样稿；检查分析方案后再授权生图。
5. 如有任务处于提交结果未知状态，恢复原请求记录，不要通过换服务或反复点击绕过未决状态。

模型细节见[模型配置](models.md)，页面使用见[使用说明](user-guide.md)。
