#!/usr/bin/env python3
"""Bounded publication check. This is a guard, not a proof of secret absence."""
import argparse
import json
import re
import subprocess
from pathlib import Path

BLOCKED_PARTS = {'.venv', 'node_modules', '.local-data', '.local-models', 'qc', 'work', '.agents', '__pycache__'}
BLOCKED_NAMES = {'AGENTS.md', 'cloud-providers.json', 'cloud-verification.json'}
BLOCKED_SUFFIXES = {'.key', '.pem', '.sqlite', '.sqlite3', '.db', '.log', '.zip', '.safetensors', '.ckpt'}
SECRET = re.compile(r'(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
PERSONAL_PATH = re.compile(r'(?:/Users/|/home/)[A-Za-z0-9._-]+/')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tracked', action='store_true', help='Inspect only the exact tracked tree')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    command = ['git', 'ls-files', '-z']
    if not args.tracked:
        command += ['--cached', '--others', '--exclude-standard']
    paths = sorted(set(subprocess.check_output(command, cwd=root).decode().split('\0')) - {''})
    issues = []
    for name in paths:
        path = Path(name)
        full = root / path
        if (BLOCKED_PARTS.intersection(path.parts) or path.name in BLOCKED_NAMES or
                path.suffix.lower() in BLOCKED_SUFFIXES or
                path.name.startswith('.env') and path.name != '.env.example' or
                tuple(path.parts[:2]) == ('frontend', 'dist')):
            issues.append({'file': name, 'reason': 'private_or_generated_path'})
            continue
        if full.is_symlink() or not full.is_file() or full.stat().st_size > 2 * 1024 * 1024:
            issues.append({'file': name, 'reason': 'non_regular_or_oversized_file'})
            continue
        try:
            body = full.read_text(encoding='utf-8')
        except UnicodeError:
            issues.append({'file': name, 'reason': 'unreviewed_binary'})
            continue
        if SECRET.search(body):
            issues.append({'file': name, 'reason': 'credential_pattern'})
        if PERSONAL_PATH.search(body):
            issues.append({'file': name, 'reason': 'personal_absolute_path'})
    print(json.dumps({'files_checked': len(paths), 'issues': issues}, ensure_ascii=False, indent=2))
    return 1 if issues or not paths else 0


if __name__ == '__main__':
    raise SystemExit(main())
