"""Isolated cloud authorization and agent bridge lifecycle counterexamples."""
import copy
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from backend.app.main import create_app, snapshot, unresolved
from backend.app.models import ModelError
from backend.app.privacy import public_error
from backend.tests.test_service import FakeText, FakeComfy, docs_double, ready, wait


class FakeCloud:
    def __init__(self):
        self.calls = 0
        self.unknown = False
        self.requests = {}
        self.download_fail = False

    def submit(self, target, prompt, key):
        self.calls += 1
        if self.unknown:
            raise TimeoutError('PRIVATE_SECRET_CANARY')
        self.requests[key] = 'cloud-' + str(self.calls)
        return self.requests[key]

    def find_submission(self, key):
        return self.requests.get(key)

    def result(self, request_id, destination):
        if self.download_fail:
            raise ModelError('PRIVATE_SECRET_CANARY')
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (96, 64), 'blue').save(destination)
        return destination


@pytest.fixture
def harness(tmp_path, docs_double):
    text, comfy = FakeText(), FakeComfy()
    app = create_app(tmp_path, text, comfy)
    app.state.service.cloud = FakeCloud()
    app.state.service.cloud_config.update('ark', {'base_url': 'https://ark.example/v3',
        'model': 'test-model', 'enabled': True, 'api_key': 'PRIVATE_SECRET_CANARY'})
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        client.headers.update({'X-Local-Token': client.get('/api/bootstrap').json()['token']})
        yield client, app.state.service, text, comfy
        app.state.service.executor.shutdown(wait=True)


def approved(h, provider='codex'):
    task = ready(h)
    response = h[0].post('/api/tasks/' + task['id'] + '/approve', json={
        'version': task['plan']['version'], 'confirm': True, 'image_provider': provider,
        'cloud_quality': 'high', 'max_attempts': 3})
    assert response.status_code == 200, response.text
    return response.json()


def start(h, task, key='cloud-test'):
    payload = {'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': key}
    response = h[0].post('/api/tasks/' + task['id'] + '/generate', json=payload)
    assert response.status_code == 200, response.text
    current, job = wait(h[0], task['id'], response.json()['job_id'])
    return current, job, payload


def identity(task, job):
    return {'job_id': job['id'], 'slot_id': job['slots'][0]['id'], 'approval_id': job['approval_id']}


def png():
    b = io.BytesIO()
    metadata = PngImagePlugin.PngInfo(); metadata.add_text('prompt', 'PRIVATE_SECRET_CANARY')
    Image.new('RGB', (96, 64), 'red').save(b, format='PNG', pnginfo=metadata)
    return b.getvalue()


def result(h, task, body):
    return h[0].post('/api/tasks/' + task['id'] + '/agent-image-result', data=body,
        files={'image': ('actual.png', png(), 'image/png')})


def test_cloud_approval_needs_no_local_translation_or_nodes(harness):
    harness[3].region_available = harness[3].mask_available = False
    task = approved(harness, 'ark')
    private = harness[1].require(task['id'])
    assert harness[2].prompt_calls == 0
    assert task['approval']['image_target']['size'] == '2048x2048'
    assert task['approval']['image_targets'][task['plan']['slots'][0]['id']]
    assert private['prompts'] and 'prompts' not in task
    assert 'PRIVATE_SECRET_CANARY' not in json.dumps(task)


def test_api_success_and_cached_download_resume_make_one_post(harness):
    task = approved(harness, 'ark')
    harness[1].cloud.download_fail = True
    task, job, payload = start(harness, task)
    assert job['status'] == 'failed' and harness[1].cloud.calls == 1
    assert 'PRIVATE_SECRET_CANARY' not in json.dumps(task)
    harness[1].cloud.download_fail = False
    response = harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload)
    task, job = wait(harness[0], task['id'], response.json()['job_id'])
    assert job['status'] == 'completed' and len(task['assets']) == 1
    assert harness[1].cloud.calls == 1


def test_unknown_cannot_replace_or_reapprove_or_repeat_post(harness):
    task = approved(harness, 'ark'); harness[1].cloud.unknown = True
    task, job, payload = start(harness, task)
    assert job['status'] == 'failed'
    assert harness[0].post('/api/tasks/' + task['id'] + '/generate', json={**payload, 'idempotency_key': 'other'}).status_code == 409
    assert harness[0].post('/api/tasks/' + task['id'] + '/approve', json={'version': task['plan']['version'], 'confirm': True, 'image_provider': 'codex', 'max_attempts': 3}).status_code == 409
    response = harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload)
    wait(harness[0], task['id'], response.json()['job_id'])
    assert harness[1].cloud.calls == 1


def test_changed_configuration_requires_reauthorization(harness):
    task = approved(harness, 'ark')
    harness[1].cloud_config.update('ark', {'model': 'new-model'})
    task, job, _ = start(harness, task)
    assert job['status'] == 'failed' and not job['submissions']
    assert harness[1].cloud.calls == 0


def test_agent_claim_once_wrong_identity_and_metadata_free_completion(harness):
    task, job, payload = start(harness, approved(harness))
    assert job['status'] == 'awaiting_agent'
    body = identity(task, job)
    assert result(harness, task, body).status_code == 409
    assert harness[0].post('/api/tasks/' + task['id'] + '/agent-image-claim', json={**body, 'slot_id': 'wrong'}).status_code == 409
    assert harness[0].post('/api/tasks/' + task['id'] + '/agent-image-claim', json=body).status_code == 200
    assert harness[0].post('/api/tasks/' + task['id'] + '/agent-image-claim', json=body).status_code == 409
    assert result(harness, task, {**body, 'approval_id': 'wrong'}).status_code == 409
    response = result(harness, task, body); assert response.status_code == 200
    saved = response.json(); asset = saved['assets'][0]
    path = harness[1].directory(task['id']) / 'assets' / (asset['id'] + '.png')
    old = path.read_bytes()
    with Image.open(path) as image: assert image.info == {}
    assert result(harness, task, body).status_code == 200
    assert path.read_bytes() == old and len(harness[1].require(task['id'])['assets']) == 1
    assert 'PRIVATE_SECRET_CANARY' not in json.dumps(saved)


def test_agent_pending_reuse_and_restart_preserve_frozen_intent(harness):
    task, job, payload = start(harness, approved(harness))
    before = copy.deepcopy(harness[1].require(task['id']))
    same = harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload)
    assert same.json()['job_id'] == job['id']
    assert harness[0].post('/api/tasks/' + task['id'] + '/generate', json={**payload, 'idempotency_key': 'new'}).status_code == 409
    harness[1].recover()
    assert harness[1].require(task['id'])['jobs'] == before['jobs']
    assert harness[1].cloud.calls == 0


@pytest.mark.parametrize('claimed', [False, True])
def test_agent_cancel_unclaimed_vs_claimed(harness, claimed):
    task, job, _ = start(harness, approved(harness))
    body = identity(task, job)
    if claimed:
        assert harness[0].post('/api/tasks/' + task['id'] + '/agent-image-claim', json=body).status_code == 200
    harness[0].post('/api/tasks/' + task['id'] + '/cancel', json={})
    private = harness[1].require(task['id'])
    assert private['approval']['attempts_used'] == (1 if claimed else 0)
    response = result(harness, task, body)
    assert response.status_code == (200 if claimed else 409)
    final = harness[1].require(task['id'])
    assert final['jobs'][-1]['status'] == 'cancelled'
    assert not unresolved(final)


def test_legacy_local_snapshot_absence_is_preserved(harness):
    task = ready(harness)
    response = harness[0].post('/api/tasks/' + task['id'] + '/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'confirm': True, 'max_attempts': 3})
    assert response.status_code == 200
    private = harness[1].require(task['id']); snap = snapshot(private)
    assert 'image_target' not in snap and 'image_targets' not in snap
    harness[0].get('/api/tasks/' + task['id'])
    assert harness[1].require(task['id']) == private


@pytest.mark.parametrize('message', [
    '云端模型尚未开通，请在供应商控制台检查开通状态后重新授权。',
    '云端模型或接入点不可用，请核对供应商配置后重新授权。',
    '云端生图参数被拒绝，请核对供应商要求后重新授权。',
    '云端密钥验证失败，请检查供应商密钥后重新授权。',
    '云端模型访问权限不足，请检查供应商权限后重新授权。'])
def test_cloud_diagnosis_is_fixed_and_never_accepts_raw_suffix(message):
    assert public_error(message) == message
    assert 'PRIVATE_SECRET_CANARY' not in public_error(message + ' PRIVATE_SECRET_CANARY')


@pytest.mark.parametrize('generation,enabled,expected', [
    ('untested', True, 'local'), ('rejected', True, 'local'),
    ('unknown', True, 'local'), ('succeeded', True, 'ark'),
    ('succeeded', False, 'local'),
])
def test_default_requires_actual_current_verification_and_never_changes_approval(harness, monkeypatch, generation, enabled, expected):
    task = approved(harness, 'ark')
    frozen = copy.deepcopy(harness[1].require(task['id']))
    catalog = harness[1].cloud_config.list_public()
    catalog['profiles'][0]['ready'] = enabled
    catalog['profiles'][0]['verification'] = {'connection': 'ok', 'model_listed': True, 'generation': generation}
    monkeypatch.setattr(harness[1].cloud_config, 'list_public', lambda: copy.deepcopy(catalog))
    boot = harness[0].get('/api/bootstrap').json()
    assert boot['defaults']['image_provider'] == expected
    assert harness[1].require(task['id']) == frozen
    assert harness[1].cloud.calls == 0


def test_all_fixed_cloud_diagnostics_survive_projection_but_raw_suffixes_do_not():
    from backend.app.cloud import PUBLIC_CLOUD_MESSAGES
    for message in PUBLIC_CLOUD_MESSAGES:
        assert public_error(message) == message
        assert 'PRIVATE_SECRET_CANARY' not in public_error(message + ' PRIVATE_SECRET_CANARY')


def test_recent_connection_failure_disqualifies_api_from_new_default(harness, monkeypatch):
    catalog = harness[1].cloud_config.list_public()
    catalog['profiles'][0]['verification'] = {'connection': 'failed', 'model_listed': False, 'generation': 'succeeded'}
    monkeypatch.setattr(harness[1].cloud_config, 'list_public', lambda: copy.deepcopy(catalog))
    assert harness[0].get('/api/bootstrap').json()['defaults']['image_provider'] == 'local'
    assert harness[1].cloud.calls == 0
