"""Definite prompt preparation failure must not masquerade as an image unknown."""
import copy
import hashlib
import json

import pytest
import httpx
from fastapi import HTTPException

from backend.app.models import ModelError
from backend.app.main import prompt_failure_receipts
from backend.app.one_click import MESSAGES
from backend.app.privacy import public_task
from backend.tests.test_one_click import harness, imported, start, terminal


def test_private_diagnostics_drop_untrusted_metadata_bodies_and_bad_counter_types():
    canary = 'PRIVATE_METADATA_BODY'
    rows = prompt_failure_receipts([{
        'phase': canary, 'done_reason': canary, 'done': canary,
        'eval_count': canary, 'prompt_eval_count': True, 'total_duration': -1,
        'input_sha256': canary, 'repaired_fields': ['text', canary],
        'field_checks': {'text': {'type': canary, 'length': canary, 'blank': canary,
                                 'has_cjk': True, 'has_ascii': False}, canary: {'type': 'string'}},
        'raw_body': canary}, None])
    assert canary not in json.dumps(rows)
    assert rows == [{'field_checks': {'text': {'has_cjk': True, 'has_ascii': False}}}]


def test_known_conversion_failure_is_terminal_without_image_or_replay(harness):
    client, service, text, comfy = harness
    def rejected(*args):
        text.prompt_calls += 1
        raise ModelError('独立画面提示词未得到有效英文，请重新转换。PRIVATE_PROMPT_SENTINEL')
    text.prompts = rejected
    task = imported(client)
    start(client, task)
    task = terminal(client, task['id'])
    assert task['automation']['status'] == task['status'] == 'failed'
    assert task['automation']['message'] == MESSAGES['prompt_failed']
    assert 'PRIVATE_PROMPT_SENTINEL' not in json.dumps(task)
    assert task['approval'] is None and not task['assets'] and not task['exports']
    private = service.require(task['id'])
    assert private['prompt_failure_evidence'][-1]['error_sha256'] == hashlib.sha256(
        '独立画面提示词未得到有效英文，请重新转换。PRIVATE_PROMPT_SENTINEL'.encode()).hexdigest()
    assert '_prompt_preparation_failure' not in task
    assert private['automation']['_actions']['approval'] == {'state': 'failed', 'code': 'prompt_preparation_failed'}
    assert private['plan'] and not private['_prompting']
    for _ in range(3):
        service.one_click.pump()
        service.one_click.recover()
    assert text.prompt_calls == 1 and comfy.calls == 0
    # User can explicitly enter the existing manual flow; no automatic retry.
    response = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'confirm': True, 'version': task['plan']['version'], 'max_attempts': 3,
        'image_provider': 'local', 'checkpoint': 'fake-checkpoint'})
    assert response.status_code == 422 and text.prompt_calls == 2 and comfy.calls == 0


@pytest.mark.parametrize('kind', ['untyped_exception', 'configuration_conflict', 'recorded_action'])
def test_unproven_or_conflicting_failure_keeps_unknown_boundary(harness, kind):
    client, service, text, comfy = harness
    original = service.one_click.actions['approval']
    def broken(task_id, body):
        if kind == 'recorded_action':
            original(task_id, body)
            raise HTTPException(422, 'post-save error')
        if kind == 'configuration_conflict':
            raise HTTPException(409, 'configuration changed')
        raise RuntimeError('PRIVATE_UNKNOWN_FAILURE')
    service.one_click.actions['approval'] = broken
    task = imported(client)
    start(client, task)
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'paused'
    assert task['automation']['message'] == MESSAGES['unknown']
    assert task['assets'] == [] and comfy.calls == 0
    if kind == 'recorded_action':
        assert service.require(task['id'])['automation']['_actions']['approval']['state'] == 'recorded'


def test_legacy_explanation_is_corrected_without_mutation_or_replay(harness):
    client, service, text, comfy = harness
    task = imported(client)
    task['plan'] = {'version': 1, 'slots': [], 'model': 'fake-text'}
    task['error'] = '独立画面提示词未得到有效英文，请重新转换。'
    task['automation'] = {'id': '00000000-0000-4000-8000-000000000000', 'status': 'paused', 'stage': 'approval',
                          'message': MESSAGES['unknown'], '_actions': {'approval': {'state': 'intent'}}}
    before = copy.deepcopy(task)
    assert public_task(task)['automation']['message'] == MESSAGES['prompt_failed']
    assert task == before and text.prompt_calls == comfy.calls == 0
    task['error'] = '提示词请求超时，响应尚未确认'
    assert public_task(task)['automation']['message'] == MESSAGES['unknown']
    task['error'] = before['error']
    task['automation']['_consent'] = {'preparation_failure_protocol': 1}
    assert public_task(task)['automation']['message'] == MESSAGES['unknown']
    task['automation'].pop('_consent')
    task['_prompt_preparation_failure'] = {'kind': 'model_validation'}
    assert public_task(task)['automation']['message'] == MESSAGES['unknown']
    task.pop('_prompt_preparation_failure')
    # An image submission makes the preparation-only explanation inapplicable.
    task['jobs'].append({'id': 'job', 'kind': 'generation', 'status': 'failed', 'submissions': []})
    assert public_task(task)['automation']['message'] == MESSAGES['unknown']


@pytest.mark.parametrize('exception', [RuntimeError('PRIVATE_UNTYPED'), httpx.ReadTimeout('PRIVATE_TIMEOUT')])
def test_real_approval_wrapper_preserves_unproven_model_failures(harness, exception):
    client, service, text, comfy = harness
    def uncertain(*args):
        text.prompt_calls += 1
        raise exception
    text.prompts = uncertain
    task = imported(client)
    start(client, task)
    task = terminal(client, task['id'])
    assert task['automation']['status'] == 'paused'
    assert task['automation']['message'] == MESSAGES['unknown']
    private = service.require(task['id'])
    assert private.get('_prompt_preparation_failure') is None
    assert private['automation']['_actions']['approval']['state'] == 'intent'
    assert comfy.calls == 0 and text.prompt_calls == 1
