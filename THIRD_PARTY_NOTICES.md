# Third-party notices

This repository contains the Tuxu Community application and public basic templates.
Dependencies are installed from their upstream distributions; dependency binaries,
model weights and third-party Codex plugins/skills are not vendored here.

The Apache-2.0 license of this application does not replace upstream licenses.
Retain the notices supplied by dependencies when distributing a packaged product.

## Direct runtime and development dependencies

| Component | Upstream license |
| --- | --- |
| FastAPI, Pydantic, python-docx, React, React DOM, Vite | MIT |
| Starlette, Uvicorn, HTTPX, lxml | BSD-3-Clause |
| Pillow | MIT-CMU |
| TypeScript | Apache-2.0 |

Pinned Python packages are listed in `requirements.lock.txt`; the complete frontend
dependency graph and integrity hashes are in `frontend/package-lock.json`. The
installed distributions include the authoritative license texts and additional
notices for their transitive dependencies. This table is an overview, not a grant
of rights to every optional component a user may install.

## Models, services and media

- Ollama and ComfyUI run as separately installed services and carry their own
  licenses. No upstream source, extensions or model checkpoints are bundled.
- Model weights can have distinct license, acceptable-use and commercial terms;
  obtain them from the model publisher and check those terms before use.
- Cloud API access, generated media rights, pricing and model availability follow
  the selected supplier's terms. No API key or free generation entitlement is included.
- Codex host collaboration requires a supported host and its image tool; this
  repository does not include or redistribute that service.
- Users must have permission for articles, fonts, reference images and other assets
  they supply. No third-party poster/prompt/photography skill library is included.
