#!/usr/bin/env python3
"""Configure a fixed local runner at the terminal, never through browser input."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.paths import default_data_directory
from backend.app.local_agent import atomic_json, LocalAgentBridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--codex', type=Path, help='Absolute path to Codex CLI 0.166.0 or newer')
    choice.add_argument('--argv-json', help='Fixed local runner argv as a JSON array; no shell')
    parser.add_argument('--data-dir', type=Path, default=None)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--enable', action='store_true', help='Opt in to dispatch after approval')
    args = parser.parse_args()
    directory = args.data_dir or default_data_directory()
    if args.codex:
        if not args.codex.is_absolute():
            parser.error('--codex must be absolute')
        config = {'mode': 'codex-native', 'argv': [sys.executable, str(ROOT / 'scripts' / 'codex_image_runner.py'),
                                                 '--codex', str(args.codex)]}
    else:
        try:
            argv = json.loads(args.argv_json)
        except ValueError:
            parser.error('--argv-json must be valid JSON')
        config = {'mode': 'local-runner', 'argv': argv}
    config.update(enabled=args.enable, timeout_seconds=args.timeout)
    # Validate using the same runtime rules before replacing the owner's config.
    class Directory:
        pass
    service = Directory(); service.store = Directory(); service.store.directory = directory
    bridge = LocalAgentBridge(service)
    temporary = directory / 'local-agent-check.json'
    try:
        atomic_json(temporary, config)
        bridge.path = temporary
        state = bridge.status()
        if not state['available']:
            parser.error(state['message'])
        atomic_json(directory / 'local-agent.json', config)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(state, ensure_ascii=False))


if __name__ == '__main__':
    main()
