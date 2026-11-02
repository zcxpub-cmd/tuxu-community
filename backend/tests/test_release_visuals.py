"""Publication exceptions are exact-path media receipts, never generic binaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct

import pytest

from scripts import check_release as release


def _media(suffix):
    if suffix == '.webp':
        payload = b'VP8 ' + struct.pack('<I', 8) + b'\x00\xff' * 4
        return b'RIFF' + struct.pack('<I', len(payload) + 4) + b'WEBP' + payload
    return struct.pack('>I', 24) + b'ftypisom\x00\x00\x02\x00isommp42' + b'\x00\xff' * 8


def _write_manifest(root, *, change=None):
    rows = []
    for name in sorted(release.VISUAL_PATHS):
        data = (root / name).read_bytes()
        width, height = release.VISUAL_DIMENSIONS[Path(name).suffix]
        rows.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'width': width, 'height': height})
    manifest = {'schema_version': 1, 'assets': rows}
    if change:
        change(manifest)
    (root / release.VISUAL_MANIFEST).write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / 'repository'
    (root / release.VISUAL_ROOT).mkdir(parents=True)
    for name in release.VISUAL_PATHS:
        (root / name).write_bytes(_media(Path(name).suffix))
    _write_manifest(root)
    return root, [release.VISUAL_MANIFEST, *sorted(release.VISUAL_PATHS)]


def _reasons(root, names):
    return {issue['reason'] for issue in release.check_paths(root, names)['issues']}


def test_complete_exact_media_set_passes_static_receipts(tree):
    root, names = tree
    assert release.check_paths(root, names) == {'files_checked': 17, 'issues': []}


def test_text_only_tree_still_needs_no_visual_manifest(tmp_path):
    (tmp_path / 'README.md').write_text('Public instructions.\n', encoding='utf-8')
    assert release.check_paths(tmp_path, ['README.md'])['issues'] == []


@pytest.mark.parametrize('name', ['other.bin', 'frontend/public/visuals/extra.webp', 'frontend/public/visuals/extra.mp4', 'frontend/public/visuals/DNA.webp'])
def test_unknown_binary_never_inherits_media_exception(tree, name):
    root, names = tree
    full = root / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(_media('.webp'))
    assert 'unreviewed_binary' in _reasons(root, [*names, name])


def test_control_bytes_cannot_pass_as_utf8_text(tmp_path):
    (tmp_path / 'unknown.bin').write_bytes(b'UTF8-looking\x00binary')
    assert _reasons(tmp_path, ['unknown.bin']) == {'unreviewed_binary'}


def test_manifest_must_be_present_in_the_checked_release_set(tree):
    root, names = tree
    without_manifest = [name for name in names if name != release.VISUAL_MANIFEST]
    assert 'visual_manifest_missing' in _reasons(root, without_manifest)
    (root / release.VISUAL_MANIFEST).unlink()
    assert 'visual_manifest_invalid' in _reasons(root, names)


def test_all_sixteen_media_must_belong_to_the_checked_release_set(tree):
    root, names = tree
    assert 'visual_asset_missing_from_release' in _reasons(root, names[:-1])


def test_changed_media_content_is_rejected_by_hash(tree):
    root, names = tree
    target = root / f'{release.VISUAL_ROOT}/dna.webp'
    data = target.read_bytes()
    target.write_bytes(data[:-1] + b'\x01')
    assert 'visual_manifest_hash_mismatch' in _reasons(root, names)


def test_changed_manifest_hash_is_rejected(tree):
    root, names = tree
    _write_manifest(root, change=lambda manifest: manifest['assets'][0].update(sha256='0' * 64))
    assert 'visual_manifest_hash_mismatch' in _reasons(root, names)


def test_manifest_size_must_match_exact_file_bytes(tree):
    root, names = tree
    _write_manifest(root, change=lambda manifest: manifest['assets'][0].update(bytes=manifest['assets'][0]['bytes'] + 1))
    assert 'visual_manifest_size_mismatch' in _reasons(root, names)


@pytest.mark.parametrize('suffix', ['.webp', '.mp4'])
def test_wrong_signature_rejected_even_with_matching_hash_and_size(tree, suffix):
    root, names = tree
    full = root / f'{release.VISUAL_ROOT}/dna{suffix}'
    full.write_bytes(b'X' * len(full.read_bytes()))
    _write_manifest(root)
    assert 'visual_signature_mismatch' in _reasons(root, names)


@pytest.mark.parametrize('suffix', ['.webp', '.mp4'])
def test_media_size_upper_bound_cannot_be_overridden_by_manifest(tree, suffix):
    root, names = tree
    full = root / f'{release.VISUAL_ROOT}/dna{suffix}'
    full.write_bytes(_media(suffix) + b'\x00' * release.VISUAL_LIMITS[suffix])
    _write_manifest(root)
    reasons = _reasons(root, names)
    assert 'visual_manifest_invalid' in reasons
    assert 'non_regular_or_oversized_file' in reasons


@pytest.mark.parametrize('suffix', ['.webp', '.mp4'])
def test_leaf_media_symlink_rejected(tree, tmp_path, suffix):
    root, names = tree
    full = root / f'{release.VISUAL_ROOT}/dna{suffix}'
    outside = tmp_path / ('external' + suffix)
    outside.write_bytes(full.read_bytes())
    full.unlink()
    full.symlink_to(outside)
    assert 'non_regular_or_oversized_file' in _reasons(root, names)


def test_parent_directory_symlink_rejected(tree, tmp_path):
    root, names = tree
    directory = root / release.VISUAL_ROOT
    outside = tmp_path / 'external-visuals'
    directory.rename(outside)
    directory.symlink_to(outside, target_is_directory=True)
    reasons = _reasons(root, names)
    assert 'visual_manifest_invalid' in reasons and 'non_regular_or_oversized_file' in reasons


def test_symlink_manifest_rejected(tree, tmp_path):
    root, names = tree
    manifest = root / release.VISUAL_MANIFEST
    outside = tmp_path / 'external-manifest.json'
    manifest.rename(outside)
    manifest.symlink_to(outside)
    assert 'visual_manifest_invalid' in _reasons(root, names)


@pytest.mark.parametrize('mutation', [
    lambda data: data.update(schema_version=True),
    lambda data: data.update(prompt='not a public media receipt'),
    lambda data: data['assets'].pop(),
    lambda data: data['assets'].append(deepcopy(data['assets'][0])),
    lambda data: data['assets'][0].update(path='frontend/public/visuals/unknown.webp'),
    lambda data: data['assets'][0].update(width=1920),
    lambda data: data['assets'][0].update(height=True),
    lambda data: data['assets'][0].update(bytes=True),
    lambda data: data['assets'][0].update(sha256='invalid'),
    lambda data: data['assets'][0].update(task_id='private-job-not-allowed'),
    lambda data: data['assets'].__setitem__(0, deepcopy(data['assets'][1])),
])
def test_manifest_schema_is_fixed_and_contains_only_public_receipt_fields(tree, mutation):
    root, names = tree
    _write_manifest(root, change=mutation)
    assert 'visual_manifest_invalid' in _reasons(root, names)


def test_duplicate_json_fields_cannot_hide_discarded_manifest_content(tree):
    root, names = tree
    full = root / release.VISUAL_MANIFEST
    original = full.read_text(encoding='utf-8')
    full.write_text('{"assets":[{"prompt":"discarded non-receipt content"}],' + original[1:], encoding='utf-8')
    assert 'visual_manifest_invalid' in _reasons(root, names)


def test_original_sensitive_path_and_text_guards_remain_enforced(tree):
    root, names = tree
    blocked = ['work/private.md', '.env', 'auth.json', 'private.key', 'frontend/dist/index.html']
    for name in blocked:
        full = root / name
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text('synthetic', encoding='utf-8')
    (root / 'unsafe.txt').write_text('sk-' + 'x' * 30 + '\n/' + 'Users/' + 'example/', encoding='utf-8')
    issues = release.check_paths(root, [*names, *blocked, 'unsafe.txt'])['issues']
    assert all(any(issue['file'] == name and issue['reason'] == 'private_or_generated_path' for issue in issues) for name in blocked)
    assert {'credential_pattern', 'personal_absolute_path'} <= {issue['reason'] for issue in issues}


@pytest.mark.parametrize('text,reason', [
    ('sk-' + 'x' * 30, 'credential_pattern'),
    ('/' + 'Users/' + 'example/', 'personal_absolute_path'),
])
def test_media_metadata_does_not_bypass_global_sensitive_text_checks(tree, text, reason):
    root, names = tree
    full = root / f'{release.VISUAL_ROOT}/dna.mp4'
    full.write_bytes(full.read_bytes() + text.encode('ascii'))
    _write_manifest(root)
    assert reason in _reasons(root, names)


def test_public_visual_manifest_matches_actual_media_bytes():
    root = Path(__file__).resolve().parents[2]
    names = [release.VISUAL_MANIFEST, *release.VISUAL_PATHS]
    assert release.check_paths(root, names)['issues'] == []


def _jpeg(width=1265, height=900, *, marker=0xc0):
    frame = bytes([8]) + struct.pack('>HH', height, width) + bytes([3]) + b'\x01\x11\x00\x02\x11\x00\x03\x11\x00'
    return b'\xff\xd8\xff\xe0\x00\x04JF' + bytes([0xff, marker]) + struct.pack('>H', len(frame) + 2) + frame + b'\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9'


def _write_screenshot_manifest(root, *, change=None):
    rows = []
    for name in sorted(release.SCREENSHOT_PATHS):
        data = (root / name).read_bytes()
        width, height = release.jpeg_dimensions(data)
        rows.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'width': width, 'height': height})
    manifest = {'schema_version': 1, 'assets': rows}
    if change:
        change(manifest)
    (root / release.SCREENSHOT_MANIFEST).write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


@pytest.fixture
def screenshot_tree(tmp_path):
    root = tmp_path / 'screenshots-repository'
    (root / release.SCREENSHOT_ROOT).mkdir(parents=True)
    for name in release.SCREENSHOT_PATHS:
        data = _jpeg(1425, 2284) if name.endswith('/director.jpg') else _jpeg()
        (root / name).write_bytes(data)
    _write_screenshot_manifest(root)
    return root, [release.SCREENSHOT_MANIFEST, *sorted(release.SCREENSHOT_PATHS)]


def test_screenshot_receipts_cover_exact_fourteen_paths_and_actual_dimensions(screenshot_tree):
    root, names = screenshot_tree
    assert len(release.SCREENSHOT_PATHS) == 14
    assert release.check_paths(root, names) == {'files_checked': 15, 'issues': []}
    assert release.jpeg_dimensions(_jpeg(1425, 2284)) == (1425, 2284)
    assert release.jpeg_dimensions(_jpeg(1265, 900, marker=0xc2)) == (1265, 900), 'Progressive screenshots retain their real dimensions'


def test_screenshot_and_visual_groups_are_independent(screenshot_tree):
    root, names = screenshot_tree
    (root / release.VISUAL_ROOT).mkdir(parents=True)
    for name in release.VISUAL_PATHS:
        (root / name).write_bytes(_media(Path(name).suffix))
    _write_manifest(root)
    both = [*names, release.VISUAL_MANIFEST, *release.VISUAL_PATHS]
    assert release.check_paths(root, both)['issues'] == []
    assert 'screenshot_manifest_missing' in _reasons(root, [name for name in both if name != release.SCREENSHOT_MANIFEST])
    assert 'visual_manifest_missing' in _reasons(root, [name for name in both if name != release.VISUAL_MANIFEST])


@pytest.mark.parametrize('name', ['docs/screenshots/unknown.jpg', 'docs/screenshots/director.jpeg', 'docs/screenshots/home-unknown.jpg', 'elsewhere.jpg'])
def test_unknown_jpeg_paths_never_receive_screenshot_exception(screenshot_tree, name):
    root, names = screenshot_tree
    full = root / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(_jpeg())
    assert 'unreviewed_binary' in _reasons(root, [*names, name])


def test_screenshot_manifest_and_all_files_must_be_in_release_set(screenshot_tree):
    root, names = screenshot_tree
    assert 'screenshot_manifest_missing' in _reasons(root, [name for name in names if name != release.SCREENSHOT_MANIFEST])
    assert 'screenshot_asset_missing_from_release' in _reasons(root, names[:-1])


def test_screenshot_hash_size_and_dimensions_must_match(screenshot_tree):
    root, names = screenshot_tree
    _write_screenshot_manifest(root, change=lambda data: data['assets'][0].update(sha256='0' * 64))
    assert 'screenshot_manifest_hash_mismatch' in _reasons(root, names)
    _write_screenshot_manifest(root, change=lambda data: data['assets'][0].update(bytes=data['assets'][0]['bytes'] + 1))
    assert 'screenshot_manifest_size_mismatch' in _reasons(root, names)
    _write_screenshot_manifest(root, change=lambda data: data['assets'][0].update(width=1600))
    assert 'screenshot_dimensions_mismatch' in _reasons(root, names)


def test_changed_jpeg_bytes_cannot_reuse_screenshot_receipt(screenshot_tree):
    root, names = screenshot_tree
    full = root / 'docs/screenshots/article.jpg'
    data = full.read_bytes()
    full.write_bytes(data[:6] + b'XX' + data[8:])
    assert 'screenshot_manifest_hash_mismatch' in _reasons(root, names)


@pytest.mark.parametrize('mutation', [
    lambda data: data.update(prompt='not a screenshot receipt'),
    lambda data: data.update(schema_version=True),
    lambda data: data['assets'].pop(),
    lambda data: data['assets'][0].update(path='docs/screenshots/unknown.jpg'),
    lambda data: data['assets'][0].update(width=16),
    lambda data: data['assets'][0].update(height=65535),
    lambda data: data['assets'][0].update(width=True),
    lambda data: data['assets'][0].update(bytes=release.SCREENSHOT_LIMIT + 1),
    lambda data: data['assets'][0].update(task_id='nonpublic'),
    lambda data: data['assets'].__setitem__(0, deepcopy(data['assets'][1])),
])
def test_screenshot_manifest_refuses_unknown_fields_unbounded_dimensions_and_duplicates(screenshot_tree, mutation):
    root, names = screenshot_tree
    _write_screenshot_manifest(root, change=mutation)
    assert 'screenshot_manifest_invalid' in _reasons(root, names)


def test_screenshot_duplicate_json_keys_rejected(screenshot_tree):
    root, names = screenshot_tree
    full = root / release.SCREENSHOT_MANIFEST
    full.write_text('{"assets":[],' + full.read_text(encoding='utf-8')[1:], encoding='utf-8')
    assert 'screenshot_manifest_invalid' in _reasons(root, names)


@pytest.mark.parametrize('bad', [
    b'not a JPEG image',
    b'\xff\xd8arbitrary fake\xff\xd9',
    _jpeg()[:-2],
    b'\xff\xd8\xff\xe0\x7f\xfftruncated\xff\xd9',
    b'\xff\xd8\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xff\xd9',
])
def test_jpeg_signature_and_bounded_header_validation(screenshot_tree, bad):
    root, names = screenshot_tree
    full = root / 'docs/screenshots/article.jpg'
    manifest = json.loads((root / release.SCREENSHOT_MANIFEST).read_text(encoding='utf-8'))
    full.write_bytes(bad)
    row = next(item for item in manifest['assets'] if item['path'].endswith('/article.jpg'))
    row.update(bytes=len(bad), sha256=hashlib.sha256(bad).hexdigest())
    (root / release.SCREENSHOT_MANIFEST).write_text(json.dumps(manifest), encoding='utf-8')
    assert 'screenshot_signature_mismatch' in _reasons(root, names)


def test_screenshot_maximum_bytes_cannot_be_overridden_by_manifest(screenshot_tree):
    root, names = screenshot_tree
    full = root / 'docs/screenshots/article.jpg'
    data = _jpeg()
    full.write_bytes(data[:-2] + b'\x00' * release.SCREENSHOT_LIMIT + data[-2:])
    _write_screenshot_manifest(root)
    reasons = _reasons(root, names)
    assert 'screenshot_manifest_invalid' in reasons and 'non_regular_or_oversized_file' in reasons


@pytest.mark.parametrize('target', ['docs/screenshots/article.jpg', release.SCREENSHOT_MANIFEST])
def test_screenshot_file_or_manifest_symlink_rejected(screenshot_tree, tmp_path, target):
    root, names = screenshot_tree
    full = root / target
    outside = tmp_path / ('outside-' + full.name)
    full.rename(outside)
    full.symlink_to(outside)
    assert 'non_regular_or_oversized_file' in _reasons(root, names)


def test_screenshot_parent_symlink_rejected(screenshot_tree, tmp_path):
    root, names = screenshot_tree
    full = root / release.SCREENSHOT_ROOT
    outside = tmp_path / 'outside-screenshots'
    full.rename(outside)
    full.symlink_to(outside, target_is_directory=True)
    assert 'screenshot_manifest_invalid' in _reasons(root, names)


@pytest.mark.parametrize('text,reason', [
    ('sk-' + 'x' * 30, 'credential_pattern'),
    ('/' + 'Users/' + 'example/', 'personal_absolute_path'),
])
def test_jpeg_metadata_still_obeys_global_sensitive_checks(screenshot_tree, text, reason):
    root, names = screenshot_tree
    full = root / 'docs/screenshots/article.jpg'
    data = full.read_bytes()
    full.write_bytes(data[:-2] + text.encode('ascii') + data[-2:])
    _write_screenshot_manifest(root)
    assert reason in _reasons(root, names)


def test_public_screenshot_manifest_matches_actual_jpeg_bytes():
    root = Path(__file__).resolve().parents[2]
    names = [release.SCREENSHOT_MANIFEST, *release.SCREENSHOT_PATHS]
    assert release.check_paths(root, names)['issues'] == []
