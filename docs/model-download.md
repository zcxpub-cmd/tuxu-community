# 文字模型下载来源

图序社区版 v0.5.0 提供官方模型来源链接，模型权重由使用者另行下载。GitHub 源码包与 Release 不附带权重、Ollama 运行时、账号或 API 密钥。文章分析和提示词使用本机文字模型；图片生成仍需单独配置具备真实生图能力的智能体或服务。

## Hugging Face 官方上游

Qwen 团队发布的 [Qwen/Qwen2.5-VL-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct) 是当前默认模型的基础来源。模型卡标注 **Apache-2.0**；下载文件与使用方法见同页及[官方文件列表](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct/tree/main)。使用或再分发时应保留上游署名与适用许可。

Hugging Face 上游提供原始 Safetensors 权重，[配置标记为 BF16](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct/blob/main/config.json)，文件总量约 16.6 GB。它与下述 Ollama 量化版属于同一基础模型，**不是相同的量化文件或相同字节**。原始 BF16 权重、计算缓存和运行时需要更高资源，不能套用图序文档中针对 Q4_K_M / 8K 上下文的 16 GB 电脑配置估算。

当前图序后端通过 Ollama 协议调用模型。仅下载上游 Safetensors 文件不会自动安装到图序，也不能直接替代 Ollama 的量化模型包；若选择上游原始权重，应按上游说明另行准备兼容的运行环境。

## 图序默认的 Ollama 量化版本

[Ollama 官方 qwen2.5vl:7b 页面](https://ollama.com/library/qwen2.5vl:7b) 当前标注 **8.29B、Q4_K_M、约 6.0 GB**，并提供 Apache 2.0 许可、模板与参数信息。安装并启动 [Ollama](https://ollama.com/download) 后，在终端执行：

```sh
ollama pull qwen2.5vl:7b
ollama show qwen2.5vl:7b
```

第一条会从 Ollama 下载模型；第二条用于核对本机实际模型信息，不代表已经完成文章分析验收。随后在图序中选择 `qwen2.5vl:7b`。电脑配置和当前调用参数见[文字模型与硬件](text-model-hardware.md)。本版图序以 macOS/Linux 为支持范围，Windows 使用 WSL，不作原生 Windows 验收承诺。

截至 2026-11-02，本轮核对的官方 Ollama manifest 为 `5ced39dfa4bac325dc183dd1e4febaa1c46b3ea28bce48896c8e69c1e79611cc`。Ollama 标签可能随上游更新，安装后应核对实际版本；本说明不承诺未来同名标签的字节不变。
