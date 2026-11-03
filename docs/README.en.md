# Tuxu Community v0.6.0

Tuxu is a local-first article illustration workbench. Import DOCX, Markdown or TXT, review illustration suggestions, adjust simple visual direction, approve generation, select an image and export a new document.

The UI offers style and brightness choices, optional backgrounds and rough object placement. Export formats are DOCX, a Markdown image bundle and HTML. Image cleanup uses a median filter and Lanczos resizing, not AI super-resolution.

Version 0.6 adds ten animated homepage scenes, bringing the collection to eighteen. The app picks a scene when you open or return to the homepage and avoids repeats on consecutive visits. The new five-second clips use a brief fade through their first frame when replaying. The homepage uses a still image when motion is disabled. [Release notes](release-v0.6.0.md)

For a newly imported article, one-click creation can cover local analysis, image generation, automatic selection and document export within a single authorization. The limit is five images with one attempt per image. The automatic flow stops after a failure or an uncertain result. The workspace also offers manual creation and light, dark or system appearance. [Workflow guide](one-click-appearance.md)

## Run locally

Use macOS/Linux, Python 3.12+ and Node.js 22.18+. Native Windows is not supported; Windows users can use a Linux environment in WSL.

```bash
./setup.sh
./start.sh
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766). Use `./stop.sh` to stop the app. Data uses a separate `TuxuCommunity` directory; set `TUXU_DATA_DIR` before startup to choose another directory. Environment files are not loaded automatically.

Article analysis requires Ollama. See the [official Hugging Face upstream model](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct), the [Ollama Q4_K_M package](https://ollama.com/library/qwen2.5vl:7b), and [model download/setup](model-download.md). These share the base model but have different formats and memory requirements. No model weights are attached to this release. Image generation can use your own cloud API credentials, local ComfyUI, Codex, or a configured generic command/local HTTP agent with actual image tooling. No accounts or API credentials are included.

## Scope

This repository uses Apache-2.0 and includes public, general-purpose prompt templates and workflow code. Advanced private templates and hosted commercial services are not included or required. The UI shows brief descriptions rather than full generation text; this is not source-code secrecy.

AI images can contain missing objects, malformed geometry and incorrect text. Bounding boxes guide composition and do not guarantee pixel-accurate placement. Scientific and news images need human review and clear illustration labels. Complex DOCX formatting and pagination may change on export; preserve your original file.

Read the [Chinese quick start](../README.md), [installation](installation.md), [model setup](models.md), [development](development.md), [contribution guide](contributing.md) and [security policy](security.md). Never include API keys, private documents or task databases in public reports.


Version 0.2 adds semantic style suggestions marked as recommended, fourteen visual styles, and an opt-in fixed local-agent runner. Ollama composes image prompts for every route. Native Codex mode requires compatible image tooling and the user's own ChatGPT sign-in; local orchestration does not mean offline or free image inference. Every generated illustration keeps the parenthetical AI label in its caption; no duplicate disclosure paragraph is added. See [local-agent setup](local-agent.md) and [public prompt design](prompt-design.md).
