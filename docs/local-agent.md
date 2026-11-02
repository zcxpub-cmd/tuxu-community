# 本机智能体自动生图

社区版可将用户批准后的图位自动交给本机固定智能体，接收真实图片并写回任务。默认没有配置，页面显示“未配置或不可用”。安装 Codex、登录账号、使用网页和验证实际生图能力是不同状态；配置成功不代表某个账号或模型已经完成真实生图验证。

分析文章、组织画面描述和生成正式图片提示词均由本地 Ollama 完成。本机智能体仅收到当前图位已冻结的图片提示词和尺寸/质量选项，不会收到整篇原稿。用户须先确认当前方案与生成次数。网页只能启用或停用本机已经配置的 runner，不能填写命令、参数或文件路径。

## 配置 Codex 原生智能体

用项目 Python 环境运行以下命令，把路径替换为本机实际路径。使用绝对路径可避免终端与桌面应用 PATH 指向不同 Codex。CLI 至少需要 0.166.0；更旧或不兼容版本会显示不可用。

```bash
.venv/bin/python scripts/configure_local_agent.py --codex /absolute/path/to/codex
```

该命令只写 Community 数据目录下权限为 `0600` 的 `local-agent.json`，默认停用。在网页“生图服务设置”中打开开关后，重新批准当前方案并点击生成。命令行也可用 `--enable` 明确启用。指定独立数据目录时加 `--data-dir /absolute/community/data`，必须与当前 Community 服务使用的目录一致。

原生 runner 通过固定绝对路径启动 `codex app-server --stdio`，创建一个临时会话和一次生成轮次，临时覆盖推理强度为 `high`、执行模型为 `gpt-6.1-sol`。对 `code_mode_only` 模型，通过 `features.code_mode` 配置表将 `image_gen` 命名空间设为直接调用，保留 Code Mode host 停用。启动时检查有效配置确实保留该表与图片命名空间；不接受仅设置一个无效的顶层字段。部分模型仍会显示其他工具声明，声明不等于调用；runner 拒绝将非原生图片调用或结果当作图片成功。不修改全局 Codex 配置，应用不读取、复制或分发账号 key；Codex 通过正常登录机制使用用户已有 ChatGPT 登录态，不接受网页传入 API key。启动先检查 `account/read` 的账户类型，只接受 `chatgpt`，拒绝 API key 或其他认证类型，邮箱和账户详情不写入日志。原生子进程不继承常见供应商 API key、API 路由或父智能体的工具管道与会话环境变量（保留用户配置的 `CODEX_HOME`），不提供兼容 API 回退。使用原生账号仍受本机 Codex 登录状态、服务权限和工具支持约束。CLI 0.166.0 不支持 `--ignore-user-config`；runner 临时关闭已验证支持的 shell、exec、多智能体、应用、插件、技能发现、浏览器和电脑操作能力，读取有效配置中的 MCP 名称后逐项临时禁用并验证，不更改全局文件。这属于收窄工具能力，不能称为所有本地文件或凭据的绝对隔离。

runner 明确要求图片工具原样使用冻结提示词并只生成一次，拒绝命令、MCP、网页或文件编辑工具结果作为图片。只有原生 `imageGeneration` 完成事件中的图片内容可回填；文本答复、普通文件名和占位图均不计成功。兼容普通 function 和 namespaced custom 原生调用，正常用户文本回显及工具定义注册不当作工具执行。启用原始 Responses 事件，至少观察到一条与冻结提示词完全一致的原生图片工具入参或原生 `revisedPrompt` 才能导入；改写或缺少这项证据均保留未知状态，绝不重调生成。批准的尺寸是名义画幅参考，真实宽高由原生工具决定；导入前要求宽高比与批准参考相差不超过 5%。`quality=auto` 表示原生自选，不承诺高/中质量参数。不能据此承诺严格像素尺寸或某个具体图片模型型号。

## 其他固定本机 runner

现在提供两种通用适配器：命令行和本机 HTTP，见下面的配置方式。已有遵守 v1 协议的 runner 仍可直接使用。

在本机终端配置固定 argv，执行过程使用 `shell=False`：

```bash
.venv/bin/python scripts/configure_local_agent.py --argv-json '["/absolute/python", "/absolute/my_image_runner.py"]'
```

`local-runner` 模式不会标为 Codex 原生能力。runner 应连接用户授权的真实本机后端。它从 stdin 读取以下协议 JSON；当前工作目录是应用创建的私有请求目录。不得重新编写 `prompt`、调用云端文本模型或执行网页提供的命令。

```json
{
  "protocol": "tuxu-local-image-v1",
  "request_id": "stable-request-id",
  "approval_id": "approval-id",
  "prompt": "approved frozen image prompt",
  "prompt_sha256": "sha256-of-exact-utf8-prompt",
  "options": {"size": "1024x1024", "quality": "high"}
}
```

真实生图完成后，在当前目录写入 `image.png`，再原子写入 `result.json`：

```json
{
  "status": "completed",
  "request_id": "same-request-id",
  "prompt_sha256": "same-prompt-hash",
  "image": "image.png"
}
```

应用验证请求身份、当前授权、冻结提示词摘要、图片可解码及尺寸限制，并清除 PNG 文本/EXIF 信息后发布资产。不接受结果指定的任意图片路径。智能体进程的 stdout/stderr 不返回网页，私有请求正文不加入导出。

## 通用命令行与本机 HTTP 适配器

接入不限定品牌。具有 CLI、可调用的本机 HTTP 服务，或可写 v1 桥接程序的智能体均可扩展。该智能体还必须配置实际生图工具并返回真实图片；仅返回文字的聊天程序不会被图序计为生图成功。Claude、Gemini、OpenCode 等名称不代表本项目已经完成各自版本/账号/图片工具的实测。只有图形界面、没有可调用接口的智能体仍需专门适配。

先复制并修改 `examples/local-agent/command.json` 或 `http.json`。命令模板中的 `--prompt-file`、`--output` 是示意参数，须按所接智能体的真实 CLI 修改，不能原样假定各品牌都支持。然后在本机终端运行：

```bash
.venv/bin/python scripts/configure_local_agent.py --adapter-config /absolute/path/my-agent.json
```

配置会私有复制到 Community 数据目录，默认停用；网页检查显示名称后再启用。终端加 `--enable` 可明确启用。不会因加入这些适配器自动替换已有 Codex 连接。`--label` 可另设短显示名；不要把路径、提示词或凭据放进名称。

### 命令行

`kind=command` 使用固定 `argv`、`shell=False`。可选择 `stdin=prompt`（精确提示词）、`request_json`（完整 v1 请求，默认）或 `none`（用私有文件）。argv 只支持四个文件/目录占位符：`{prompt_file}`、`{request_file}`、`{output_file}`、`{work_dir}`；不支持 `{prompt}` 或 shell 拼接。提示词不会作为命令行参数出现。

智能体应在当前私有请求目录的 `image.png` 写入单张 PNG，并以退出码 0 结束。包装程序验证、去元数据并写原子的 `result.json`。超时由外层图序进程组统一终止；本包装器仅通过图序使用。该智能体需有在指定目录写图的实际能力；具体插件/生图工具由用户在智能体内配置。

### 本机 HTTP

`kind=http` 只接受带端口的字面 `http(s)://127.0.0.1:端口/路径` 或 `[::1]`，不接受 DNS、URL 认证、查询/fragment、重定向、环境代理或自动重试。请求正文是上面的 `tuxu-local-image-v1` JSON；本机服务返回：

```json
{
  "status": "completed",
  "request_id": "same-request-id",
  "prompt_sha256": "same-prompt-hash",
  "image_base64": "base64-encoded-single-PNG"
}
```

不接受图片 URL、任意文件路径或纯文字替代图片。HTTP 适配器没有密钥配置槽；带认证或特殊 SDK 的服务可用自写 v1 runner 处理。loopback 只限制图序连接的地址，服务自身仍可能使用云端工具，并不等于完全离线。配置检查不请求该服务，连接是否真正能返回图片要另做经授权的实际验收。

### 修改和证据边界

适配器配置、包装模块、命令中可识别的绝对程序/脚本路径、`revision_files` 显式列出的依赖均以内容摘要绑定批准。另行加载的插件/配置、HTTP 服务的版本标识可用 `revision_files` 声明；不能自动识别所有传递依赖或服务内部模型。文件内容变化需重新批准。`revision_files` 不要包含账号凭据或日志。

领取时为当前请求保存私有配置快照，执行进程在分发前再次校验已批准的程序内容摘要。领取后修改常用配置文件不会将该请求改派给新命令；已变更的程序文件会阻止执行。文件属于本机拥有者的可信配置范围，这不构成针对管理员实时替换可执行文件的操作系统级隔离。

命令/HTTP 运行前独占并持久化 `adapter-started.json`，同一请求不能再次分发；超时/异常/坏图只保留待核查。回执里的 hash 证明请求关联，不是第三方图片工具的完整调用审计，不能据此保证其内部没有重写提示词或重试。本次新增适配器使用隔离的合成 PNG 做协议验证，没有把夹具当作实际智能体生成证据。

## 中断、取消与恢复

应用先持久化唯一 claim，再运行一个固定进程。已提交的画面不会因超时、失败、点击重复生成、切换开关或重启再次调用智能体。状态未知时保留原请求待核查。只有新执行保存了完整已完成轮次，且原生/旧版图片事件、原始工具调用及未知工具事件全部为零，才可将未实际开始生图的请求标为 `not_started` 并退回未使用的次数；缺少这项持久证据的旧请求保持未知。重启可导入原请求目录中已存在且有效的真实图片和完成回执，不能重新开始该请求。原生 runner 还使用一次性 `started.json`，阻止同目录重复运行。

取消会停止尚未领取的后续图位；已经领取的画面仍可保存返回结果。应用退出会终止当前本机进程，重启后仍保留“待核查”，不会重新生图。已领取请求不因更换 runner 自动重试；尚未领取时更换 runner 或其程序内容，会使原批准失效并退回未执行的尝试次数，需要重新确认方案。开关启停不改变 runner 身份。

旧版人工 claim/result 接口保留供本机兼容操作。未配置自动 runner 时页面明确显示不可用，不会声称正在自动使用当前聊天。测试中的合成图片仅验证协议、持久化和清除元数据，不能作为真实 AI 生图或画面质量证据。
