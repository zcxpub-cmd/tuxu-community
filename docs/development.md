# 开发说明

## 目录

| 路径 | 用途 |
| --- | --- |
| `frontend/src` | React/TypeScript 工作台、导演台、公开类型与恢复守卫 |
| `frontend/tests` | 生成幂等键、文档预览、导演台、云配置和公开视图检查 |
| `backend/app` | FastAPI、任务存储、模型适配、公开基础模板、文档和媒体处理 |
| `backend/tests` | 模型协议、授权、恢复、隐私投影、网络与导出检查 |
| `scripts` | 本地启动辅助和任务 CLI |
| `docs` | 安装、使用、模型、贡献与安全说明 |

高级私有模板和第三方插件/技能库不是本项目的构建依赖。

## 本地开发

先按[安装说明](installation.md)安装依赖。在第一个终端启动后端：

```bash
./start.sh
```

在第二个终端启动前端开发服务：

```bash
cd frontend
npm run dev
```

以终端输出的前端地址为准。Vite 的 `/api` 默认代理到 `http://127.0.0.1:8766`；更改后端端口时需同步修改开发代理。正式本地使用由后端提供构建后的前端。

开发、测试和个人使用应使用不同的 `TUXU_DATA_DIR`。不要把自己的任务目录作为测试夹具，不要在仓库中写入真实密钥。

## 前端检查

```bash
cd frontend
npm ci
npm run build
npm run test:keys
npm run test:preview
npm run test:director
npm run test:public
npm run test:providers
npm run test:verification
```

`test:public` 同时检查生产构建，先执行 `build`。测试直接读取 TypeScript 源文件，因此需要支持类型剥离的 Node.js 版本，见[安装要求](installation.md)。构建通过只证明代码可构建，不等于真实模型画面或文档显示通过。

## 后端检查

在仓库根目录运行，并显式隔离数据：

```bash
TUXU_DATA_DIR="$(mktemp -d)" .venv/bin/python -m pytest -q backend/tests
```

一般改动先跑相关文件；准备提交或发布时再执行项目 CI 要求的检查。协议测试应使用清楚标识的 Mock，不能拿 Mock 图片宣称模型效果。真实 API 测试需要开发者自己的明确授权与预算，不在常规测试中自动执行。

## 行为约定

- 修改方案、模型或配置后，原授权不能继续支配新的生成请求。
- 未决提交恢复原请求，不自动重发、换供应商或把中断当成明确拒绝。
- 云配置的候选列表只带入输入框；保存、连接查询与实际生成分别记录。
- 前端只读取公开投影和简述；完整任务内部数据、密钥与供应商原始错误正文不能直接回传。
- 通用社区模板是公开源码。若新增模板，确认贡献权利，不引入私有策略或复制外部插件。
- 导入原稿保持只读；导出副本由用户复核编号、图位和分页。

支持宿主生图桥时，使用领取/冻结/回填标识，不旁路网页批准或重复调用已领取任务。可从 CLI 的 `--help` 阅读可用参数；CLI 与服务必须指向相同数据目录。
