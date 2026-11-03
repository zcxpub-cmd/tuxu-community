#!/usr/bin/env python3
"""Bounded publication check. This is a guard, not a proof of secret absence."""
import argparse
import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path

BLOCKED_PARTS = {'.venv', 'node_modules', '.local-data', '.local-models', 'qc', 'work', '.agents', '__pycache__',
                 'local-agent-requests', 'local-agent-runs', '.agent-runtime', 'model-dist'}
BLOCKED_NAMES = {'AGENTS.md', 'cloud-providers.json', 'cloud-verification.json', 'local-agent.json',
                 'local-agent-adapter.json', 'local-agent-check.json', 'auth.json'}
BLOCKED_SUFFIXES = {'.key', '.pem', '.sqlite', '.sqlite3', '.db', '.log', '.zip', '.safetensors', '.ckpt', '.gguf'}
SECRET = re.compile(r'(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
PERSONAL_PATH = re.compile(r'(?:/Users/|/home/)[A-Za-z0-9._-]+/')
BINARY_SECRET = re.compile(SECRET.pattern.encode('ascii'))
BINARY_PERSONAL_PATH = re.compile(PERSONAL_PATH.pattern.encode('ascii'))
LEGACY_SCENE_IDS = ('blackhole', 'fold', 'stellar', 'city', 'field', 'mountain', 'ocean', 'dna')
MOTION_SCENE_IDS = ('street', 'village', 'forest', 'lake', 'desert', 'rice', 'milkyway', 'cells', 'molecules', 'garden')
VISUAL_ROOT = 'frontend/public/visuals'
VISUAL_MANIFEST = f'{VISUAL_ROOT}/manifest.json'
VISUAL_LIMITS = {'.webp': 512 * 1024, '.mp4': 4 * 1024 * 1024}
VISUAL_DIMENSIONS = {
    f'{VISUAL_ROOT}/{scene}{suffix}': dimensions
    for scene in LEGACY_SCENE_IDS
    for suffix, dimensions in {'.webp': (1672, 941), '.mp4': (1280, 720)}.items()
}
VISUAL_DIMENSIONS.update({
    f'{VISUAL_ROOT}/{scene}{suffix}': (1012, 720)
    for scene in MOTION_SCENE_IDS for suffix in VISUAL_LIMITS
})
VISUAL_PATHS = frozenset(VISUAL_DIMENSIONS)
SCREENSHOT_ROOT = 'docs/screenshots'
SCREENSHOT_MANIFEST = f'{SCREENSHOT_ROOT}/manifest.json'
SCREENSHOT_NAMES = tuple(f'home-{scene}' for scene in LEGACY_SCENE_IDS) + (
    'article', 'plan', 'director', 'authorization', 'image-review', 'document-export',
)
SCREENSHOT_PATHS = frozenset(f'{SCREENSHOT_ROOT}/{name}.jpg' for name in SCREENSHOT_NAMES)
SCREENSHOT_LIMIT = 1024 * 1024


def _has_symlink(root, path):
    return any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1))


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_manifest_key')
        value[key] = item
    return value


def _media_manifest(root, names, issues, *, allowed_paths, manifest_name, limits, dimension_check, kind):
    if not (allowed_paths.intersection(names) or manifest_name in names):
        return {}
    if manifest_name not in names:
        issues.append({'file': manifest_name, 'reason': f'{kind}_manifest_missing'})
        return {}
    path = Path(manifest_name)
    full = root / path
    try:
        if _has_symlink(root, path) or not full.is_file() or full.stat().st_size > 64 * 1024:
            raise ValueError
        manifest = json.loads(full.read_text(encoding='utf-8'), object_pairs_hook=_unique_json_object)
        if not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'assets'} or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1:
            raise ValueError
        rows = manifest['assets']
        if not isinstance(rows, list) or len(rows) != len(allowed_paths):
            raise ValueError
        entries = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'path', 'sha256', 'bytes', 'width', 'height'}:
                raise ValueError
            name = row['path']
            if not isinstance(name, str) or name not in allowed_paths or name in entries:
                raise ValueError
            suffix = Path(name).suffix
            if (not isinstance(row['sha256'], str) or re.fullmatch(r'[a-f0-9]{64}', row['sha256']) is None or
                    type(row['bytes']) is not int or not 0 < row['bytes'] <= limits[suffix] or
                    type(row['width']) is not int or type(row['height']) is not int or
                    not dimension_check(name, row['width'], row['height'])):
                raise ValueError
            entries[name] = row
        if set(entries) != allowed_paths:
            raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError):
        issues.append({'file': manifest_name, 'reason': f'{kind}_manifest_invalid'})
        return {}
    for name in sorted(allowed_paths.difference(names)):
        issues.append({'file': name, 'reason': f'{kind}_asset_missing_from_release'})
    return entries


def _visual_manifest(root, names, issues):
    return _media_manifest(root, names, issues, allowed_paths=VISUAL_PATHS,
        manifest_name=VISUAL_MANIFEST, limits=VISUAL_LIMITS, kind='visual',
        dimension_check=lambda name, width, height: (width, height) == VISUAL_DIMENSIONS[name])


def _screenshot_dimensions_ok(_name, width, height):
    return 640 <= width <= 2560 and 360 <= height <= 4096 and width * height <= 8 * 1024 * 1024


def _screenshot_manifest(root, names, issues):
    return _media_manifest(root, names, issues, allowed_paths=SCREENSHOT_PATHS,
        manifest_name=SCREENSHOT_MANIFEST, limits={'.jpg': SCREENSHOT_LIMIT},
        dimension_check=_screenshot_dimensions_ok, kind='screenshot')


def _sensitive_binary_issue(data):
    if BINARY_SECRET.search(data):
        return 'credential_pattern'
    if BINARY_PERSONAL_PATH.search(data):
        return 'personal_absolute_path'
    return None


def jpeg_dimensions(data):
    """Read a bounded JPEG SOF header; this is not a codec/visual-quality validator."""
    if len(data) < 12 or data[:2] != b'\xff\xd8' or data[-2:] != b'\xff\xd9':
        raise ValueError('invalid_jpeg_signature')
    offset = 2
    sof_markers = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    while offset < len(data) - 2:
        if data[offset] != 0xff:
            raise ValueError('invalid_jpeg_marker')
        while offset < len(data) - 2 and data[offset] == 0xff:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker in {0x00, 0xd8, 0xd9, 0xda} or offset + 2 > len(data) - 2:
            raise ValueError('jpeg_missing_sof')
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            continue
        length = struct.unpack('>H', data[offset:offset + 2])[0]
        if length < 2 or offset + length > len(data) - 2:
            raise ValueError('invalid_jpeg_segment')
        if marker in sof_markers:
            if length < 8:
                raise ValueError('invalid_jpeg_sof')
            height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
            components = data[offset + 7]
            if data[offset + 2] != 8 or not 1 <= components <= 4 or length != 8 + 3 * components or not width or not height:
                raise ValueError('invalid_jpeg_sof')
            return width, height
        offset += length
    raise ValueError('jpeg_missing_sof')


def _screenshot_issue(full, entry):
    if entry is None:
        return 'screenshot_manifest_entry_missing'
    data = full.read_bytes()
    if len(data) != entry['bytes']:
        return 'screenshot_manifest_size_mismatch'
    try:
        dimensions = jpeg_dimensions(data)
    except ValueError:
        return 'screenshot_signature_mismatch'
    if dimensions != (entry['width'], entry['height']):
        return 'screenshot_dimensions_mismatch'
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        return 'screenshot_manifest_hash_mismatch'
    return _sensitive_binary_issue(data)


def _visual_issue(full, suffix, entry):
    if entry is None:
        return 'visual_manifest_entry_missing'
    data = full.read_bytes()
    if len(data) != entry['bytes']:
        return 'visual_manifest_size_mismatch'
    if suffix == '.webp':
        valid = len(data) >= 20 and data[:4] == b'RIFF' and data[8:12] == b'WEBP' and struct.unpack('<I', data[4:8])[0] + 8 == len(data)
    else:
        valid = len(data) >= 16 and data[4:8] == b'ftyp' and 16 <= struct.unpack('>I', data[:4])[0] <= len(data)
    if not valid:
        return 'visual_signature_mismatch'
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        return 'visual_manifest_hash_mismatch'
    # ASCII secrets/absolute paths in metadata remain prohibited inside approved media.
    return _sensitive_binary_issue(data)


def check_paths(root, paths):
    """Inspect an exact release file set; approved media do not exempt text guards."""
    root = Path(root)
    paths = sorted(set(paths))
    issues = []
    visuals = _visual_manifest(root, set(paths), issues)
    screenshots = _screenshot_manifest(root, set(paths), issues)
    for name in paths:
        path = Path(name)
        full = root / path
        if path.is_absolute() or '..' in path.parts or name != path.as_posix() or '\\' in name:
            issues.append({'file': name, 'reason': 'unsafe_release_path'})
            continue
        if (BLOCKED_PARTS.intersection(path.parts) or path.name in BLOCKED_NAMES or
                path.suffix.lower() in BLOCKED_SUFFIXES or
                path.name.startswith('.env') and path.name != '.env.example' or
                tuple(path.parts[:2]) == ('frontend', 'dist')):
            issues.append({'file': name, 'reason': 'private_or_generated_path'})
            continue
        try:
            limit = VISUAL_LIMITS[path.suffix] if name in VISUAL_PATHS else SCREENSHOT_LIMIT if name in SCREENSHOT_PATHS else 2 * 1024 * 1024
            if _has_symlink(root, path) or not full.is_file() or full.stat().st_size > limit:
                issues.append({'file': name, 'reason': 'non_regular_or_oversized_file'})
                continue
            if name in VISUAL_PATHS:
                reason = _visual_issue(full, path.suffix, visuals.get(name))
                if reason:
                    issues.append({'file': name, 'reason': reason})
                continue
            if name in SCREENSHOT_PATHS:
                reason = _screenshot_issue(full, screenshots.get(name))
                if reason:
                    issues.append({'file': name, 'reason': reason})
                continue
            # The media exception is for exact paths, never an extension allowlist.
            if path.suffix.lower() in {*VISUAL_LIMITS, '.jpg', '.jpeg'}:
                issues.append({'file': name, 'reason': 'unreviewed_binary'})
                continue
            body = full.read_text(encoding='utf-8')
        except UnicodeError:
            issues.append({'file': name, 'reason': 'unreviewed_binary'})
            continue
        except OSError:
            issues.append({'file': name, 'reason': 'non_regular_or_oversized_file'})
            continue
        if any(ord(character) < 32 and character not in '\t\r\n' for character in body):
            issues.append({'file': name, 'reason': 'unreviewed_binary'})
            continue
        if SECRET.search(body):
            issues.append({'file': name, 'reason': 'credential_pattern'})
        if PERSONAL_PATH.search(body):
            issues.append({'file': name, 'reason': 'personal_absolute_path'})
    return {'files_checked': len(paths), 'issues': issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tracked', action='store_true', help='Inspect only the exact tracked tree')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    command = ['git', 'ls-files', '-z']
    if not args.tracked:
        command += ['--cached', '--others', '--exclude-standard']
    paths = sorted(set(subprocess.check_output(command, cwd=root).decode().split('\0')) - {''})
    result = check_paths(root, paths)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['issues'] or not paths else 0


if __name__ == '__main__':
    raise SystemExit(main())
