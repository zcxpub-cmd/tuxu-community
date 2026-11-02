#!/usr/bin/env python3
"""Fixed adapter entry point. Run only under the supervising Community bridge."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.agent_adapter import dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            return 2
        request = json.loads(raw)
        expected = request.get('_adapter_bundle')
        if not isinstance(expected, dict):
            return 2
        dispatch(request, args.config, Path.cwd(), expected_bundle=expected, executable=sys.executable)
    except Exception:
        # Raw exceptions can contain prompt text, paths or agent credentials.
        print('Local adapter incomplete; retain the original request for review.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
