# 电影式首页与统一工作台

首页标题为两行，**让文字，拥有色彩。** 场景库共十八套，包含黑洞、折光、星际、城市、田野、高山、大海、DNA，以及本版新增的十套。

| 新增场景 | 首帧静图 |
| --- | --- |
| 市井 | [查看](../frontend/public/visuals/street.webp) |
| 村庄 | [查看](../frontend/public/visuals/village.webp) |
| 森林 | [查看](../frontend/public/visuals/forest.webp) |
| 湖泊 | [查看](../frontend/public/visuals/lake.webp) |
| 沙漠 | [查看](../frontend/public/visuals/desert.webp) |
| 金秋稻田 | [查看](../frontend/public/visuals/rice.webp) |
| 银河星野 | [查看](../frontend/public/visuals/milkyway.webp) |
| 生物细胞 | [查看](../frontend/public/visuals/cells.webp) |
| 微观分子 | [查看](../frontend/public/visuals/molecules.webp) |
| 晨光花园 | [查看](../frontend/public/visuals/garden.webp) |

每次进入或返回首页时，从随机袋中取一个场景，避免相邻重复。浏览当前首页时保持同一场景。浏览器保存最近的场景 ID 与待用顺序。首页的标题、导航和按钮是独立的网页元素。

新增场景在对无文字底图做轻度降噪后，使用 Seedance 2.0 生成视频。随包视频为 1012 × 720、24 fps、5 秒、静音。网页播放当前场景，在重播时经过首帧静图淡化衔接。视频加载失败、工作台关闭动效，或系统启用减少动态效果时，首页显示对应静图。生物细胞和微观分子是生成的概念画面。

工作台覆盖文章导入、配图方案、简明导演台、授权生成、选图处理和文档导出，支持日间、夜间和跟随系统外观。画风推荐仍可自由更改，专业项保持折叠。返回首页保留工作区的内存状态，切换任务或重新分析前保护未保存的修改。完成标记依据当前任务记录、真实候选图片和导出状态，零图方案用“无需此步骤”表达。

社区版使用本地模型分析文章与生成提示词。页面显示简明画面描述，生成图注使用“（AI 生成示意图）”。

运行首页和工作台相关检查。

```sh
cd frontend
npm run test:scenes
npm run test:workflow
npm run build
```

发布检查按固定路径校验 `frontend/public/visuals` 中的 18 段视频及 18 张静图，核对清单、哈希、大小和格式签名。
