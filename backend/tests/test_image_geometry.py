"""Native canvas compatibility and real receipt recovery, using synthetic pixels only."""
import copy
import hashlib
import json
import uuid

import pytest
from PIL import Image, PngImagePlugin

from backend.app.image_geometry import fits_requested_canvas, native_canvas_notice
from backend.app.local_agent import LocalAgentBridge, atomic_json
from backend.app.main import unresolved
from backend.tests.test_cloud_service import harness, start
from backend.tests.test_local_agent import configure
from backend.tests.test_service import docs_double, ready


@pytest.mark.parametrize('requested,actual', [
    ('1536x1024', (1672, 941)), ('landscape', (1280, 960)),
    ('1024x1536', (941, 1672)), ('portrait', (960, 1280)),
])
def test_only_native_common_canvas_variants(requested, actual):
    assert fits_requested_canvas(requested, *actual, native=True)
    assert not fits_requested_canvas(requested, *actual, native=False)


@pytest.mark.parametrize('requested,actual', [
    ('1536x1024', (941, 1672)), ('1024x1536', (1672, 941)),
    ('1024x1024', (1672, 941)), ('1536x1024', (1024, 1024)),
    ('1536x1024', (4000, 500)), ('1024x1536', (500, 4000)),
    ('2000x1000', (1672, 941)), ('invalid', (1672, 941)),
    (None, (1672, 941)), ('1536x1024', (0, 941)),
])
def test_orientation_square_and_unsupported_canvas_remain_rejected(requested, actual):
    assert not fits_requested_canvas(requested, *actual, native=True)


def test_notice_is_safe_dimensions_only_and_unchanged_pixels_need_none():
    assert native_canvas_notice('1536x1024', 1536, 1024) is None
    assert native_canvas_notice('invalid', 1672, 941) is None
    notice = native_canvas_notice('1536x1024', 1672, 941)
    assert '1672 × 941' in notice and '1536 × 1024' in notice
    assert '完整原图' in notice


def prepare(h, tmp_path, shape='landscape', sizes=((1672, 941),), mode='codex-native'):
    client, service, *_ = h
    service.local_agent.stop()
    calls = configure(service, tmp_path, enabled=False)
    task = ready(h)
    plan = copy.deepcopy(task['plan'])
    first = plan['slots'][0]
    first['aspect_ratio'] = shape
    for _ in sizes[1:]:
        slot = copy.deepcopy(first); slot['id'] = str(uuid.uuid4()); plan['slots'].append(slot)
    response = client.put('/api/tasks/' + task['id'] + '/plan', json={
        'version': plan['version'], 'style': plan['style'], 'slots': plan['slots']})
    assert response.status_code == 200
    task = response.json()
    response = client.post('/api/tasks/' + task['id'] + '/approve', json={
        'version': task['plan']['version'], 'confirm': True, 'image_provider': 'codex', 'max_attempts': 3})
    assert response.status_code == 200
    task, job, payload = start(h, response.json())
    originals = []
    for slot, size in zip(job['slots'], sizes):
        identity = {'job_id': job['id'], 'slot_id': slot['id'], 'approval_id': job['approval_id']}
        claimed = service.claim_agent_image(task['id'], identity, local=True, mode=mode)
        work = service.local_agent.request_directory(claimed['request_id']);work.mkdir(parents=True)
        frozen = service.require(task['id'])['jobs'][-1]['prompts'][slot['id']]['text']
        digest = hashlib.sha256(frozen.encode()).hexdigest()
        atomic_json(work / 'request.json', {'protocol': 'tuxu-local-image-v1', 'request_id': claimed['request_id'],
            'approval_id': job['approval_id'], 'prompt': frozen, 'prompt_sha256': digest})
        image = Image.new('RGB', size, (20, 50, 80))
        image.putpixel((0, 0), (255, 0, 0));image.putpixel((size[0] - 1, size[1] - 1), (255, 255, 0))
        metadata = PngImagePlugin.PngInfo();metadata.add_text('prompt', 'PRIVATE_PIXEL_FIXTURE')
        image.save(work / 'image.png', pnginfo=metadata);originals.append(image)
        atomic_json(work / 'result.json', {'status': 'completed', 'request_id': claimed['request_id'],
            'prompt_sha256': digest, 'image': 'image.png'})
    return task, job, payload, originals, calls


@pytest.mark.parametrize('shape,size', [('landscape', (1672, 941)), ('portrait', (941, 1672))])
def test_completed_native_receipt_recovers_original_pixels_without_new_submission(harness, tmp_path, shape, size):
    task, job, payload, originals, calls = prepare(harness, tmp_path, shape, (size, size))
    service = harness[1];before = copy.deepcopy(service.require(task['id'])['approval'])
    bridge = LocalAgentBridge(service);bridge.pump();bridge.pump()
    saved = service.require(task['id']);assert saved['status'] == 'review'
    assert saved['jobs'][-1]['status'] == 'completed' and not unresolved(saved)
    assert len(saved['assets']) == 2 and not calls.exists()
    assert saved['approval'] == before
    for asset, source in zip(saved['assets'], originals):
        path = service.directory(task['id']) / 'assets' / (asset['id'] + '.png')
        with Image.open(path) as result:
            assert result.size == size and result.tobytes() == source.tobytes() and result.info == {}
        assert asset['native_size_notice'] and asset['stale'] is False
    public = harness[0].get('/api/tasks/' + task['id']).json()
    assert 'PRIVATE_PIXEL_FIXTURE' not in json.dumps(public)
    assert all(asset.get('native_size_notice') for asset in public['assets'])
    assert harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload).json()['job_id'] == job['id']
    assert not calls.exists()


def test_generic_runner_stays_strict_and_rejection_finishes(harness, tmp_path):
    task, _, _, _, calls = prepare(harness, tmp_path, mode='local-runner')
    LocalAgentBridge(harness[1]).pump()
    saved = harness[1].require(task['id'])
    assert saved['status'] == 'failed' and not saved['assets'] and not unresolved(saved)
    assert saved['jobs'][-1]['submissions'][0]['bridge_state'] == 'rejected'
    assert not calls.exists()
