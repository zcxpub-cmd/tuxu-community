#!/usr/bin/env python3
"""Check a loopback Community server without importing the application Store."""
import json
import sys
import urllib.request


def check(port):
    port = int(port)
    if not 1 <= port <= 65535:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/bootstrap', timeout=3) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            return False
        return json.loads(body).get('application', {}).get('id') == 'tuxu-community'
    except Exception:
        return False


if __name__ == '__main__':
    raise SystemExit(0 if check(sys.argv[1]) else 1)
