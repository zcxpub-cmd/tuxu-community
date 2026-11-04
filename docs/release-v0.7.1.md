# 图序社区版 v0.7.1 · 生图恢复与文档修复

- 本机智能体图片写入后，应用可以在重启或重复回填时结算作业。已提交请求按原标识核查，避免重复生成。
- 一键流程在等待图片时区分供应商拒绝、临时查询失败和授权配置变化。Seedream 单图请求采用官方默认的非流式和单图模式。
- Codex 原生执行器使用本机登录状态检查，继续校验工具隔离、冻结提示词和原生图片回执。横竖画幅会验证方向并显示实际分辨率。 [配置说明](local-agent.md)
- 修复无标题文件的名称、损坏 DOCX 的错误处理、无显式节信息的导出和降噪后的重复导出。手动图片检查完成后保留当前页面。取消进行中的导出时，保留用户取消状态和已返回图片。
- 开源发布包只提供项目源码和公开素材。运行时的模型权重、稿件、私有配置和 API Key 由用户另行准备。 [安装说明](installation.md) · [生图通道](models.md)

本次在 macOS 对 ComfyUI、Codex 原生智能体、豆包 Seedream 5.0 Pro 和 GPT Image 2 各完成一次真实回图、插图和 Word 导出。图片的内容及构图仍需人工核对。供应商模型权限取决于用户自己的账户。 [火山方舟图片 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh) · [OpenAI Images API](https://developers.openai.com/api/reference/resources/images/methods/generate)
