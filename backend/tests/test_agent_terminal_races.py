"""Synthetic terminal races. Run with an isolated TUXU_DATA_DIR; no real models."""
import copy
import hashlib
import io

import pytest
from PIL import Image

from backend.app.local_agent import LocalAgentBridge
from backend.app.main import create_app
from backend.tests.test_cloud_service import harness
from backend.tests.test_image_geometry import prepare
from backend.tests.test_service import docs_double


@pytest.mark.parametrize('recovery', ['restart', 'duplicate_result'])
def test_saved_result_settles_after_interrupted_finalization(harness, tmp_path, monkeypatch, recovery):
    task, job, _payload, originals, calls = prepare(harness, tmp_path)
    client, service, text, comfy = harness
    with monkeypatch.context() as interrupted:
        interrupted.setattr(service, 'finish_agent_if_settled', lambda *_: None)
        LocalAgentBridge(service).pump()
    before = service.require(task['id'])
    assert before['jobs'][-1]['status'] == 'awaiting_agent'
    assert before['jobs'][-1]['submissions'][0]['state'] == 'saved'
    assert len(before['assets']) == 1

    if recovery == 'restart':
        create_app(tmp_path, text, comfy).state.service.recover()
    else:
        image = io.BytesIO()
        originals[0].save(image, 'PNG')
        asset = before['assets'][0]
        response = client.post('/api/tasks/' + task['id'] + '/agent-image-result', data={
            'job_id': job['id'], 'slot_id': asset['slot_id'], 'approval_id': job['approval_id']},
            files={'image': ('retry.png', image.getvalue(), 'image/png')})
        assert response.status_code == 200

    after = service.require(task['id'])
    assert after['jobs'][-1]['status'] == 'completed'
    assert after['status'] == 'review'
    assert after['assets'] == before['assets']
    assert after['approval'] == before['approval']
    asset = after['assets'][0]
    assert client.post('/api/tasks/' + task['id'] + '/select', json={
        'selections': {asset['slot_id']: {'asset_id': asset['id'], 'use_processed': False}}}).status_code == 200
    assert not calls.exists()


def test_known_rejection_wins_over_late_success_after_entry_validation(harness, tmp_path, monkeypatch):
    task, job, _payload, _originals, calls = prepare(harness, tmp_path)
    service = harness[1]
    before = copy.deepcopy(service.require(task['id']))
    original_save = service.save_cloud_result
    observed = []

    def reject_before_saved_mutation(task_id, job_id, slot, submission, destination):
        current = service.require(task_id)
        current_job = next(item for item in current['jobs'] if item['id'] == job_id)
        prompt_hash = hashlib.sha256(current_job['prompts'][slot['id']]['text'].encode()).hexdigest()
        # A competing importer has a definitive rejection after this successful
        # importer passed its initial checks, but before its final Store mutation.
        assert service.local_agent._rejected_receipt(task_id, job_id, slot['id'],
            submission['request_id'], current_job['approval_id'], prompt_hash)
        observed.append(service.require(task_id)['jobs'][-1]['submissions'][0]['state'])
        return original_save(task_id, job_id, slot, submission, destination)

    monkeypatch.setattr(service, 'save_cloud_result', reject_before_saved_mutation)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    current = next(item for item in saved['jobs'] if item['id'] == job['id'])
    assert observed == ['failed']
    assert current['status'] == saved['status'] == 'failed'
    assert current['submissions'][0]['state'] == 'failed'
    assert current['submissions'][0]['bridge_state'] == 'rejected'
    assert saved['assets'] == []
    assert current['attempts'] == before['jobs'][-1]['attempts']
    assert saved['approval']['attempts_used'] == before['approval']['attempts_used']
    assert not calls.exists()


def test_saved_claim_duplicate_preserves_original_pixels_and_asset(harness, tmp_path):
    task, job, _payload, _originals, calls = prepare(harness, tmp_path)
    service = harness[1]
    LocalAgentBridge(service).pump()
    before = copy.deepcopy(service.require(task['id']))
    asset = before['assets'][0]
    path = service.directory(task['id']) / 'assets' / (asset['id'] + '.png')
    original_bytes = path.read_bytes()
    other = io.BytesIO()
    Image.new('RGB', (1672, 941), 'magenta').save(other, format='PNG')
    assert other.getvalue() != original_bytes
    body = {'job_id': job['id'], 'slot_id': job['slots'][0]['id'], 'approval_id': job['approval_id']}
    service.complete_agent_image(task['id'], body, other.getvalue())
    after = service.require(task['id'])
    assert path.read_bytes() == original_bytes
    assert after['assets'] == before['assets']
    assert after['jobs'] == before['jobs']
    assert after['approval'] == before['approval']
    assert after['status'] == before['status'] == 'review'
    assert not calls.exists()


def test_old_completion_cannot_replace_newer_active_job_status(harness, tmp_path, monkeypatch):
    task, job, _payload, _originals, calls = prepare(harness, tmp_path)
    service = harness[1]
    original_update = service.update
    switched = []
    newer_id = 'synthetic-newer-analysis-job'

    def start_successor_after_asset_save(task_id, change):
        result = original_update(task_id, change)
        current = result[0]
        old = next(item for item in current['jobs'] if item['id'] == job['id'])
        if not switched and current['assets'] and all(item['state'] == 'saved' for item in old['submissions']):
            switched.append(True)

            def successor(value):
                # Another completion wins the old job's terminal CAS; a new
                # explicitly requested analysis starts before this old caller
                # attempts its own final aggregation. The snapshot is unchanged
                # until that new analysis actually returns a revised plan.
                prior = next(item for item in value['jobs'] if item['id'] == job['id'])
                prior['status'], prior['message'], prior['error'] = 'completed', 'Synthetic competing completion', None
                prior['progress'] = 1
                value['jobs'].append({'id': newer_id, 'kind': 'analysis', 'status': 'queued',
                    'progress': 0, 'message': 'Synthetic newer analysis', 'error': None,
                    'request_ids': [], 'attempts': 0, 'submissions': []})
                value['status'], value['error'] = 'analyzing', None

            original_update(task_id, successor)
        return result

    monkeypatch.setattr(service, 'update', start_successor_after_asset_save)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    assert switched == [True]
    assert saved['status'] == 'analyzing'
    assert saved['jobs'][-1]['id'] == newer_id and saved['jobs'][-1]['status'] == 'queued'
    assert next(item for item in saved['jobs'] if item['id'] == job['id'])['status'] == 'completed'
    assert len(saved['assets']) == 1 and not calls.exists()
