# Tuxu Community v0.1.0

Tuxu is a local-first article illustration workbench. Import DOCX, Markdown or TXT, review illustration suggestions, adjust simple visual direction, approve generation, select an image and export a new document.

The UI offers style and brightness choices, optional backgrounds and rough object placement. Export formats are DOCX, a Markdown image bundle and HTML. Image cleanup uses a median filter and Lanczos resizing, not AI super-resolution.

## Run locally

Use macOS/Linux, Python 3.12+ and Node.js 22.18+. Native Windows is not supported; Windows users can use a Linux environment in WSL.

```bash
./setup.sh
./start.sh
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766). Use `./stop.sh` to stop the app. Data uses a separate `TuxuCommunity` directory; set `TUXU_DATA_DIR` before startup to choose another directory. Environment files are not loaded automatically.

Article analysis currently requires a configured Ollama model. Image generation can use your own cloud API credentials, local ComfyUI, or a supported Codex host session with an image-generation tool. A standalone browser cannot call your ChatGPT account. No API credentials or model weights are included.

## Scope

This repository uses Apache-2.0 and includes public, general-purpose prompt templates and workflow code. Advanced private templates and hosted commercial services are not included or required. The UI shows brief descriptions rather than full generation text; this is not source-code secrecy.

AI images can contain missing objects, malformed geometry and incorrect text. Bounding boxes guide composition and do not guarantee pixel-accurate placement. Scientific and news images need human review and clear illustration labels. Complex DOCX formatting and pagination may change on export; preserve your original file.

Read the [Chinese quick start](../README.md), [installation](installation.md), [model setup](models.md), [development](development.md), [contribution guide](contributing.md) and [security policy](security.md). Never include API keys, private documents or task databases in public reports.
