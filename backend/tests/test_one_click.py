"""Bounded durable orchestration with synthetic text/images; no model requests."""
import io
import json
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.one_click import MESSAGES
from backend.tests.test_service import FakeComfy, FakeText


@pytest.fixture
def harness(tmp_path):
    text, comfy = FakeText(), FakeComfy()
    app = create_app(tmp_path, text, comfy)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        client.headers['X-Local-Token'] = client.get('/api/bootstrap').json()['token']
        yield client, app.state.service, text, comfy
        if text.block:
            text.block.set()
        if comfy.block:
            comfy.block.set()
        app.state.service.executor.shutdown(wait=True)


def imported(client):
    response = client.post('/api/tasks/paste', json={'title': '合成科普短文', 'text': '树木帮助理解生态。\n\n叶片接收阳光。',
                                                   'genre': 'report', 'style': '清晰的科普插图'})
    assert response.status_code == 200, response.text
    return response.json()


def payload(task, **overrides):
    return {'confirm': True, 'idempotency_key': 'one-click:' + task['id'], 'model': 'fake-text',
            'image_provider': 'local', 'checkpoint': 'fake-checkpoint', 'cloud_quality': 'medium',
            'format': 'docx', 'max_images': 5, **overrides}


def start(client, task, **overrides):
    response = client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, **overrides))
    assert response.status_code == 200, response.text
    return response.json()


def until(client, task_id, condition, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get('/api/tasks/' + task_id).json()
        if condition(task):
            return task
        time.sleep(.02)
    raise AssertionError('Synthetic orchestration did not reach expected state: ' + json.dumps(task, ensure_ascii=False))


def terminal(client, task_id):
    return until(client, task_id, lambda task: task['automation']['status'] not in ('running', 'waiting'))


@pytest.mark.parametrize('format', ['docx', 'markdown', 'html'])
def test_real_document_chain_is_bounded_and_idempotent(harness, format):
    client, service, text, comfy = harness
    task = imported(client)
    started = start(client, task, format=format)
    duplicate = start(client, task, format=format)
    assert duplicate['automation']['id'] == started['automation']['id']
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'completed', task['automation']
    assert task['status'] == 'exported'
    assert task['automation']['completed_images'] == task['automation']['total_images'] == 1
    assert text.calls == text.prompt_calls == comfy.calls == 1
    assert len(task['jobs']) == 2 and len(task['assets']) == len(task['exports']) == 1
    assert task['approval']['max_attempts'] == task['approval']['attempts_used'] == 1
    assert task['plan']['slots'][0]['style_recommendation']['style_id'] == task['plan']['slots'][0]['director']['style_id']
    assert all(not selection['use_processed'] for selection in task['selections'].values())
    response = client.get(task['automation']['export_url'])
    assert response.status_code == 200
    if format == 'docx':
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert len([name for name in archive.namelist() if name.startswith('word/media/')]) == 1
            document = archive.read('word/document.xml').decode()
    elif format == 'markdown':
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert len([name for name in archive.namelist() if name.endswith('.png')]) == 1
            document = archive.read(next(name for name in archive.namelist() if name.endswith('.md'))).decode()
    else:
        document = response.text
        assert 'data:image/png;base64,' in document
    assert '树木帮助理解生态。' in document
    assert 'AI生成示意图' in ''.join(document.split())
    repeated = start(client, task, format=format)
    service.one_click.pump()
    assert repeated['automation']['export_url'] == task['automation']['export_url']
    assert text.calls == text.prompt_calls == comfy.calls == 1
    assert len(service.require(task['id'])['exports']) == 1


def test_zero_image_plan_exports_original_without_approval(harness):
    client, _, text, comfy = harness
    original = text.analyze
    def zero(*args):
        result = original(*args)
        result['slots'] = []
        return result
    text.analyze = zero
    task = start(client, imported(client))
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'completed'
    assert task['automation']['message'] == MESSAGES['zero']
    assert task['automation']['total_images'] == 0
    assert task['approval'] is None and task['assets'] == []
    assert text.calls == 1 and text.prompt_calls == comfy.calls == 0
    assert len(task['exports']) == 1


def test_over_authorized_analysis_never_approves_or_generates(harness):
    client, _, text, comfy = harness
    original = text.analyze
    def two(*args):
        result = original(*args)
        result['slots'].append({**result['slots'][0], 'paragraph_id': result['coverage'][1]})
        return result
    text.analyze = two
    task = start(client, imported(client), max_images=1)
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'failed'
    assert task['plan'] is None and task['approval'] is None
    assert text.calls == 1 and text.prompt_calls == comfy.calls == 0
    assert task['assets'] == task['exports'] == []


def test_active_run_blocks_manual_mutations_and_cancel_is_terminal(harness):
    client, service, text, comfy = harness
    text.block = threading.Event()
    task = start(client, imported(client))
    until(client, task['id'], lambda current: any(job['status'] == 'running' for job in current['jobs']))
    for route, method, body in [('analyze', 'post', {'model': 'fake-text'}), ('plan', 'put', {}),
                                ('approve', 'post', {}), ('generate', 'post', {}), ('select', 'post', {'selections': {}}),
                                ('export', 'post', {'confirm_insert': True, 'format': 'docx', 'idempotency_key': 'manual'})]:
        response = getattr(client, method)(f'/api/tasks/{task["id"]}/{route}', json=body)
        assert response.status_code == 409, (route, response.text)
    response = client.post(f'/api/tasks/{task["id"]}/cancel', json={})
    assert response.status_code == 200
    text.block.set()
    until(client, task['id'], lambda current: current['jobs'][0]['status'] == 'cancelled')
    for _ in range(3):
        service.one_click.pump()
    final = client.get('/api/tasks/' + task['id']).json()
    assert final['automation']['status'] == 'cancelled'
    assert final['plan'] is None and final['approval'] is None
    assert text.prompt_calls == comfy.calls == 0 and final['exports'] == []


def test_unknown_submit_pauses_and_never_retries(harness):
    client, service, text, comfy = harness
    comfy.uncertain, comfy.findable = True, False
    task = start(client, imported(client))
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'paused'
    assert task['automation']['message'] == MESSAGES['unknown']
    for _ in range(3):
        service.one_click.pump()
        start(client, task)
    assert comfy.calls == 1 and text.calls == text.prompt_calls == 1
    assert task['assets'] == task['exports'] == []


def test_target_becoming_unavailable_pauses_without_submit(harness):
    client, service, text, comfy = harness
    text.block = threading.Event()
    task = start(client, imported(client))
    until(client, task['id'], lambda current: any(job['status'] == 'running' for job in current['jobs']))
    comfy.health = lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE_TARGET_SENTINEL'))
    task = terminal(client, task['id'])
    text.block.set()
    assert task['automation']['status'] == 'paused'
    assert task['automation']['message'] == MESSAGES['changed']
    assert comfy.calls == text.prompt_calls == 0
    assert 'PRIVATE_TARGET_SENTINEL' not in json.dumps(task)


def test_comfy_health_timeout_after_dispatch_does_not_cancel_accepted_request(harness):
    client, service, _, comfy = harness
    comfy.block = threading.Event()
    task = start(client, imported(client))
    assert comfy.entered.wait(4)
    comfy.health = lambda: (_ for _ in ()).throw(RuntimeError('busy ComfyUI'))
    service.one_click.pump()
    comfy.block.set()
    final = terminal(client, task['id'])
    assert final['automation']['status'] == 'completed'
    assert len(final['assets']) == len(final['exports']) == comfy.calls == 1


def test_preflight_and_fresh_only_and_key_conflicts(harness):
    client, service, text, comfy = harness
    task = imported(client)
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, confirm=False)).status_code == 400
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, max_images=6)).status_code == 400
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, model='uninstalled')).status_code == 409
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, checkpoint='missing')).status_code == 409
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, image_provider='codex')).status_code == 409
    assert service.require(task['id']).get('automation') is None and text.calls == comfy.calls == 0
    service.update(task['id'], lambda current: current.update(plan={'version': 1, 'slots': []}))
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task)).status_code == 409
    fresh = start(client, imported(client))
    assert client.post(f'/api/tasks/{fresh["id"]}/one-click', json=payload(fresh, format='html')).status_code == 409
    assert client.post(f'/api/tasks/{fresh["id"]}/one-click', json=payload(fresh, idempotency_key='different')).status_code == 409


def test_public_automation_is_strict_projection(harness):
    client, service, _, _ = harness
    service.one_click.stop()
    task = imported(client)
    # No background run is needed to check deliberately hostile private ledger data.
    def corrupt(current):
        current['automation'] = {'id': '6d37b21e-984d-460b-a019-2381af734fb4', 'status': 'paused', 'stage': 'generation',
            'message': 'PRIVATE_LEDGER_SENTINEL', 'format': 'docx', 'max_images': 5, 'total_images': 1, 'completed_images': 0,
            '_consent': {'target': 'PRIVATE_LEDGER_SENTINEL'}, '_actions': {'approval': 'PRIVATE_LEDGER_SENTINEL'},
            'revision': 'PRIVATE_LEDGER_SENTINEL', 'export_url': 'https://PRIVATE_LEDGER_SENTINEL'}
    service.update(task['id'], corrupt)
    exposed = client.get('/api/tasks/' + task['id']).json()
    assert 'PRIVATE_LEDGER_SENTINEL' not in json.dumps(exposed)
    assert set(exposed['automation']) == {'id', 'status', 'stage', 'message', 'format', 'max_images', 'total_images', 'completed_images'}
    assert exposed['automation']['message'] == MESSAGES['unknown']


def test_cancel_keeps_late_original_but_never_selects_or_exports(harness):
    client, service, _, comfy = harness
    comfy.block = threading.Event()
    task = start(client, imported(client))
    assert comfy.entered.wait(4)
    assert client.post(f'/api/tasks/{task["id"]}/cancel', json={}).status_code == 200
    comfy.block.set()
    task = until(client, task['id'], lambda current: len(current['assets']) == 1 and current['jobs'][-1]['status'] == 'cancelled')
    service.one_click.pump()
    assert task['automation']['status'] == 'cancelled'
    assert task['approval']['attempts_used'] == comfy.calls == 1
    assert task['selections'] == {} and task['exports'] == []


def test_partial_failure_does_not_export_missing_images(harness):
    from backend.app.models import CompletedRequestError
    client, _, text, comfy = harness
    analyze, result_image = text.analyze, comfy.result
    def two(*args):
        result = analyze(*args)
        result['slots'].append({**result['slots'][0], 'paragraph_id': result['coverage'][1]})
        return result
    def second_failure(request_id, destination):
        if request_id == 'request-2':
            raise CompletedRequestError('Synthetic terminal result failure')
        return result_image(request_id, destination)
    text.analyze, comfy.result = two, second_failure
    task = start(client, imported(client))
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'failed'
    assert task['automation']['completed_images'] == len(task['assets']) == 1
    assert task['automation']['total_images'] == comfy.calls == task['approval']['attempts_used'] == 2
    assert task['exports'] == [] and task['selections'] == {}


@pytest.mark.parametrize('action_state', ['intent', 'running_analysis'])
def test_restart_pauses_indeterminate_actions_instead_of_replaying(harness, action_state):
    client, service, text, comfy = harness
    service.one_click.stop()
    service.one_click.stop_event.clear()
    service.schedule = lambda *_: None
    task = start(client, imported(client))
    run_id = task['automation']['id']
    if action_state == 'intent':
        service.update(task['id'], lambda current: current['automation']['_actions'].update(analysis={'state': 'intent'}))
    else:
        service.one_click._invoke(task['id'], run_id, 'analysis', {'model': 'fake-text'})
        service.update(task['id'], lambda current: current['jobs'][0].update(status='running'))
    replacement_text, replacement_comfy = FakeText(), FakeComfy()
    replacement = create_app(service.store.directory, replacement_text, replacement_comfy)
    with TestClient(replacement, base_url='http://127.0.0.1:8765') as reloaded:
        reloaded.headers['X-Local-Token'] = reloaded.get('/api/bootstrap').json()['token']
        final = terminal(reloaded, task['id'])
        assert final['automation']['status'] == 'paused'
        assert final['automation']['message'] == MESSAGES['unknown']
        assert final['exports'] == []
        assert text.calls == comfy.calls == replacement_text.calls == replacement_comfy.calls == 0


def test_restart_safe_queued_analysis_runs_existing_job_once(harness):
    client, service, text, comfy = harness
    service.one_click.stop()
    service.one_click.stop_event.clear()
    service.schedule = lambda *_: None
    task = start(client, imported(client))
    service.one_click._invoke(task['id'], task['automation']['id'], 'analysis', {'model': 'fake-text'})
    replacement_text, replacement_comfy = FakeText(), FakeComfy()
    replacement = create_app(service.store.directory, replacement_text, replacement_comfy)
    with TestClient(replacement, base_url='http://127.0.0.1:8765') as reloaded:
        reloaded.headers['X-Local-Token'] = reloaded.get('/api/bootstrap').json()['token']
        final = terminal(reloaded, task['id'])
        assert final['automation']['status'] == 'completed'
        assert len(final['jobs']) == 2 and len(final['exports']) == 1
        assert replacement_text.calls == replacement_text.prompt_calls == replacement_comfy.calls == 1
        assert text.calls == comfy.calls == 0


def test_changed_original_pixels_are_not_automatically_exported(harness):
    from PIL import Image
    client, service, _, _ = harness
    original_select = service.one_click.actions['selection']
    def select_then_change(task_id, body):
        response = original_select(task_id, body)
        current = service.require(task_id)
        asset = current['assets'][0]
        Image.new('RGB', (64, 64), (220, 10, 10)).save(service.directory(task_id) / 'assets' / (asset['id'] + '.png'))
        return response
    service.one_click.actions['selection'] = select_then_change
    task = start(client, imported(client))
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'paused'
    assert task['exports'] == []


def test_api_provider_uses_frozen_target_and_original_asset(harness):
    from backend.tests.test_cloud_service import FakeCloud
    client, service, text, comfy = harness
    service.cloud = FakeCloud()
    service.cloud_config.update('ark', {'base_url': 'https://ark.example/v3', 'model': 'synthetic-model',
                                      'enabled': True, 'api_key': 'SYNTHETIC_TEST_KEY'})
    task = start(client, imported(client), image_provider='ark')
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'completed'
    assert service.cloud.calls == text.calls == text.prompt_calls == 1 and comfy.calls == 0
    assert len(task['assets']) == len(task['exports']) == 1
    frozen = service.require(task['id'])['automation']['_consent']['target']['targets']['square']
    assert frozen == task['approval']['image_targets'][task['plan']['slots'][0]['id']]
    assert 'SYNTHETIC_TEST_KEY' not in json.dumps(task)


def test_cloud_rejection_after_dispatch_is_not_reported_as_changed_configuration(harness):
    from backend.app.cloud import _REJECTION_MESSAGES
    from backend.app.models import RejectedSubmissionError
    from backend.tests.test_cloud_service import FakeCloud

    client, service, _, _ = harness
    service.cloud = FakeCloud()
    service.cloud_config.update('ark', {'base_url': 'https://ark.example/v3', 'model': 'synthetic-model',
                                       'enabled': True, 'api_key': 'SYNTHETIC_TEST_KEY'})
    error_code = 'InvalidEndpointOrModel.NotFound'
    def rejected(target, prompt, submission_key):
        service.cloud.calls += 1
        epoch = service.cloud_config.capture_verification(target)
        service.cloud_config.record_generation(target, 'rejected', 404, error_code, verification_epoch=epoch)
        raise RejectedSubmissionError(_REJECTION_MESSAGES[error_code])
    service.cloud.submit = rejected

    task = terminal(client, start(client, imported(client), image_provider='ark')['id'])
    assert task['automation']['status'] == 'failed'
    assert task['automation']['message'] == MESSAGES['failed']
    assert task['jobs'][-1]['error'] == _REJECTION_MESSAGES[error_code]
    assert not task['assets'] and not task['exports'] and service.cloud.calls == 1


def test_api_target_revision_drift_stops_before_approval(harness):
    from backend.tests.test_cloud_service import FakeCloud
    client, service, text, comfy = harness
    service.cloud = FakeCloud()
    service.cloud_config.update('ark', {'base_url': 'https://ark.example/v3', 'model': 'synthetic-model',
                                      'enabled': True, 'api_key': 'SYNTHETIC_TEST_KEY'})
    text.block = threading.Event()
    task = start(client, imported(client), image_provider='ark')
    until(client, task['id'], lambda current: any(job['status'] == 'running' for job in current['jobs']))
    service.cloud_config.update('ark', {'model': 'different-synthetic-model'})
    task = terminal(client, task['id'])
    text.block.set()
    assert task['automation']['status'] == 'paused'
    assert task['automation']['message'] == MESSAGES['changed']
    assert service.cloud.calls == text.prompt_calls == comfy.calls == 0
    assert task['approval'] is None and task['exports'] == []


def test_enabled_generic_agent_can_complete_but_disabled_agent_cannot_start(harness, tmp_path):
    from backend.tests.test_local_agent import configure
    client, service, text, comfy = harness
    calls = configure(service, tmp_path, enabled=False)
    task = imported(client)
    assert client.post(f'/api/tasks/{task["id"]}/one-click', json=payload(task, image_provider='codex')).status_code == 409
    assert service.require(task['id']).get('automation') is None and not calls.exists()
    assert client.post('/api/local-agent', json={'enabled': True}).status_code == 200
    task = start(client, task, image_provider='codex')
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'completed'
    assert calls.read_text() == 'x'
    assert text.calls == text.prompt_calls == 1 and comfy.calls == 0
    assert len(task['assets']) == len(task['exports']) == 1
    assert service.require(task['id'])['approval']['local_agent_revision'] == service.require(task['id'])['automation']['_consent']['target']['agent_revision']
