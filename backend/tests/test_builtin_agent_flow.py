"""Integrated agent/OneClick boundaries; synthetic model/image outputs only."""
import json
import threading

import pytest

from backend.app.vision_review import CATEGORIES, ReviewUnavailable
from backend.tests.test_one_click import harness, imported, payload, start, terminal, until


class Inspection:
    def __init__(self, verdict='match', fail=False):
        self.verdict, self.fail = verdict, fail
        self.calls = 0
        self.entered = threading.Event()
        self.block = None

    def review(self, model, brief, encoded_image, *, before_call=None):
        if before_call:
            before_call()
        self.calls += 1
        self.entered.set()
        assert model == 'fake-text' and encoded_image and isinstance(brief, dict)
        assert 'prompt' not in brief and 'negative' not in brief
        if self.block:
            self.block.wait(5)
        if self.fail:
            raise ReviewUnavailable('SENSITIVE_PRIVATE_ERROR')
        return {'checks': [{'category': category, 'verdict': self.verdict if category == 'subject' else 'match'}
                           for category in CATEGORIES], 'evidence': {'model_calls': 1}}


def enabled(client):
    config = client.get('/api/builtin-agent').json()
    response = client.put('/api/builtin-agent', json={
        'model': 'fake-text', 'visual_review_enabled': True, 'expected_revision': config['revision']})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('verdict,fail,completed', [('match', False, True), ('mismatch', False, False),
                                                 ('uncertain', False, False), ('match', True, False)])
def test_one_click_review_gates_real_document_export(harness, verdict, fail, completed):
    client, service, text, comfy = harness
    config = enabled(client)
    vision = Inspection(verdict, fail)
    service.builtin_agent.vision = vision
    task = imported(client)
    start(client, task)
    task = terminal(client, task['id'])
    assert (task['automation']['status'] == 'completed') is completed, task['automation']
    assert len(task['exports']) == (1 if completed else 0)
    assert vision.calls == text.calls == text.prompt_calls == comfy.calls == 1
    asset = task['assets'][0]
    assert asset['visual_review']['status'] == ('passed' if completed else 'unavailable' if fail else 'needs_attention')
    private = service.require(task['id'])
    assert private['approval']['_builtin_agent']['revision'] == config['revision']
    public_text = json.dumps(task)
    for marker in ('_builtin_agent', '_builtin_visual_review', 'SENSITIVE_PRIVATE_ERROR',
                   private['prompts'][asset['slot_id']]['text']):
        assert marker not in public_text
    for _ in range(3):
        service.one_click.pump()
    assert vision.calls == comfy.calls == 1


def test_default_profile_never_adds_implicit_inspection(harness):
    client, service, text, comfy = harness
    vision = Inspection()
    service.builtin_agent.vision = vision
    assert client.get('/api/builtin-agent').json()['visual_review_enabled'] is False
    task = imported(client)
    start(client, task)
    task = terminal(client, task['id'])
    assert task['status'] == 'exported' and vision.calls == 0
    assert 'visual_review' not in task['assets'][0]


def test_manual_explicit_check_is_idempotent_after_completed_run(harness):
    client, service, text, comfy = harness
    vision = Inspection()
    service.builtin_agent.vision = vision
    task = imported(client)
    start(client, task)
    task = terminal(client, task['id'])
    path = f'/api/tasks/{task["id"]}/assets/{task["assets"][0]["id"]}/review'
    assert client.post(path, json={'confirm': True, 'model': 'UNAUTHORIZED'}).status_code == 400
    assert client.post(path, json={'confirm': True}).status_code == 200
    task = until(client, task['id'], lambda t: t['assets'][0].get('visual_review', {}).get('status') == 'passed')
    assert client.post(path, json={'confirm': True}).status_code == 200
    assert vision.calls == 1 and len(task['exports']) == 1


def test_cancel_before_review_return_never_selects_or_exports(harness):
    client, service, text, comfy = harness
    enabled(client)
    vision = Inspection()
    vision.block = threading.Event()
    service.builtin_agent.vision = vision
    task = imported(client)
    start(client, task)
    assert vision.entered.wait(5)
    try:
        response = client.post(f'/api/tasks/{task["id"]}/cancel', json={})
        assert response.status_code == 200
    finally:
        vision.block.set()
    service.executor.submit(lambda: None).result(timeout=5)
    task = client.get(f'/api/tasks/{task["id"]}').json()
    assert task['automation']['status'] == 'cancelled'
    assert task['assets'][0]['visual_review']['status'] == 'interrupted'
    assert task['selections'] == {} and task['exports'] == [] and vision.calls == comfy.calls == 1


def test_settings_have_local_token_and_revision_cas(harness):
    client, service, _, _ = harness
    before = client.get('/api/builtin-agent').json()
    body = {'model': 'fake-text', 'visual_review_enabled': True, 'expected_revision': before['revision']}
    assert client.put('/api/builtin-agent', json=body, headers={'X-Local-Token': 'wrong'}).status_code == 403
    current = client.put('/api/builtin-agent', json=body)
    assert current.status_code == 200
    assert client.put('/api/builtin-agent', json=body).status_code == 409
    assert client.get('/api/bootstrap').json()['models']['builtin_agent']['revision'] == current.json()['revision']
