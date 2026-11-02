"""Automation contract tests. Images below are fixtures, never real AI evidence."""
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from backend.app.local_agent import LocalAgentBridge, atomic_json
from backend.app.main import unresolved
from backend.tests.test_cloud_service import harness, approved, start, identity, png
from backend.tests.test_service import docs_double


def configure(service, tmp_path, behavior='success', enabled=True, timeout=5):
    runner = tmp_path / 'fixture_runner.py'
    runner.write_text('''import hashlib,json,sys,time
from pathlib import Path
from PIL import Image,PngImagePlugin
r=json.load(sys.stdin)
calls=Path(__file__).with_suffix('.calls')
calls.write_text(calls.read_text()+'x' if calls.exists() else 'x')
print('PRIVATE_SECRET_CANARY '+r['prompt'])
BEHAVIOR
'''.replace('BEHAVIOR', {
        'success': "m=PngImagePlugin.PngInfo();m.add_text('prompt',r['prompt']);Image.new('RGB',(96,96),'red').save('image.png',pnginfo=m)\nPath('result.json').write_text(json.dumps({'status':'completed','request_id':r['request_id'],'prompt_sha256':r['prompt_sha256'],'image':'image.png'}))",
        'unknown': "raise SystemExit(1)",
        'sleep': "time.sleep(10)",
        'bad_hash': "Image.new('RGB',(96,96),'red').save('image.png')\nPath('result.json').write_text(json.dumps({'status':'completed','request_id':r['request_id'],'prompt_sha256':'wrong','image':'image.png'}))"
    }[behavior]))
    atomic_json(service.local_agent.path, {'mode': 'local-runner', 'enabled': enabled,
        'argv': [sys.executable, str(runner)], 'timeout_seconds': timeout})
    return runner.with_suffix('.calls')


def finish_automatic(h, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = h[0].get('/api/tasks/' + task_id).json()
        if task['jobs'][-1]['status'] in ('completed', 'failed', 'cancelled'):
            return task
        time.sleep(.02)
    raise AssertionError('automatic fixture runner did not complete')


def test_unconfigured_status_and_browser_cannot_supply_command(harness):
    client, service, *_ = harness
    state = client.get('/api/bootstrap').json()['models']['local_agent']
    assert state['available'] is False and state['enabled'] is False
    assert '未配置' in state['message']
    assert client.get('/api/image-providers').json()['built_in']['ready'] is False
    assert client.post('/api/local-agent', json={'enabled': True}).status_code == 422
    assert client.post('/api/local-agent', json={'enabled': False, 'argv': ['/bin/sh']}).status_code == 422
    assert not service.local_agent.path.exists()
    headers = dict(client.headers)
    client.headers.pop('x-local-token')
    assert client.post('/api/local-agent', json={'enabled': False}).status_code == 403
    client.headers.update(headers)
    assert client.post('/api/local-agent', json={'enabled': False}, headers={'Origin': 'https://foreign.example'}).status_code == 403


def test_enabled_fixed_runner_is_automatic_claim_import_and_private(harness, tmp_path):
    calls = configure(harness[1], tmp_path)
    task, job, payload = start(harness, approved(harness))
    task = finish_automatic(harness, task['id'])
    private = harness[1].require(task['id'])
    assert calls.read_text() == 'x'
    assert len(task['assets']) == 1 and task['jobs'][-1]['submissions'][0]['state'] == 'saved'
    assert 'PRIVATE_SECRET_CANARY' not in json.dumps(task)
    assert private['jobs'][-1]['snapshot']['local_agent_revision'] != 'unconfigured'
    frozen = private['jobs'][-1]['prompts'][private['jobs'][-1]['slots'][0]['id']]['text']
    work = harness[1].local_agent.request_directory(private['jobs'][-1]['submissions'][0]['request_id'])
    request = json.loads((work / 'request.json').read_text())
    assert request['prompt'] == frozen and 'document' not in request and 'paragraphs' not in request
    asset = private['assets'][0]
    with Image.open(harness[1].directory(task['id']) / 'assets' / (asset['id'] + '.png')) as image:
        assert image.info == {}
    harness[1].local_agent.pump()
    assert calls.read_text() == 'x'
    assert harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload).json()['job_id'] == job['id']


def test_toggle_persists_and_disabling_does_not_change_runner_revision(harness, tmp_path):
    calls = configure(harness[1], tmp_path, enabled=False)
    before = harness[1].local_agent.revision()
    task, job, _ = start(harness, approved(harness))
    assert not calls.exists()
    response = harness[0].post('/api/local-agent', json={'enabled': True})
    assert response.status_code == 200 and response.json()['enabled'] is True
    assert harness[1].local_agent.revision() == before
    assert harness[0].get('/api/image-providers').json()['built_in']['ready'] is True
    finish_automatic(harness, task['id'])
    assert calls.read_text() == 'x'
    assert LocalAgentBridge(harness[1]).status()['enabled'] is True


@pytest.mark.parametrize('behavior', ['unknown', 'bad_hash', 'sleep'])
def test_unknown_timeout_and_invalid_receipt_never_repeat_after_restart(harness, tmp_path, behavior):
    service = harness[1]
    service.local_agent.stop()
    calls = configure(service, tmp_path, behavior=behavior, timeout=1)
    task, job, payload = start(harness, approved(harness))
    service.local_agent.stop_event.clear()
    service.local_agent.pump()
    item = service.require(task['id'])['jobs'][-1]['submissions'][0]
    assert calls.read_text() == 'x' and item['state'] == 'submitted' and item['bridge_state'] == 'unknown'
    assert unresolved(service.require(task['id']))
    service.recover()
    restarted = LocalAgentBridge(service)
    restarted.pump()
    restarted.pump()
    assert calls.read_text() == 'x' and not service.require(task['id'])['assets']
    response = harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload)
    assert response.json()['job_id'] == job['id']
    assert harness[0].post('/api/tasks/' + task['id'] + '/generate', json={**payload, 'idempotency_key': 'different'}).status_code == 409


def test_restart_imports_existing_pixels_without_invoking_runner(harness, tmp_path):
    service = harness[1]
    service.local_agent.stop()
    calls = configure(service, tmp_path)
    task, job, _ = start(harness, approved(harness))
    body = identity(task, job)
    claimed = service.claim_agent_image(task['id'], body, local=True)
    work = service.local_agent.request_directory(claimed['request_id']); work.mkdir(parents=True)
    prompt = service.require(task['id'])['jobs'][-1]['prompts'][body['slot_id']]['text']
    sha = hashlib.sha256(prompt.encode()).hexdigest()
    atomic_json(work / 'request.json', {'request_id': claimed['request_id'], 'approval_id': body['approval_id'],
                                      'prompt': prompt, 'prompt_sha256': sha})
    buffer = io.BytesIO(); Image.new('RGB', (96, 96), 'red').save(buffer, format='PNG')
    (work / 'image.png').write_bytes(buffer.getvalue())
    atomic_json(work / 'result.json', {'status': 'completed', 'request_id': claimed['request_id'],
                                      'prompt_sha256': sha, 'image': 'image.png'})
    restarted = LocalAgentBridge(service)
    restarted.pump()
    assert not calls.exists()
    saved = service.require(task['id'])
    assert len(saved['assets']) == 1 and saved['jobs'][-1]['status'] == 'completed'


def test_runner_replacement_rejects_unclaimed_approval_without_call(harness, tmp_path):
    service = harness[1]; service.local_agent.stop()
    calls = configure(service, tmp_path, enabled=False)
    task = approved(harness)
    source = tmp_path / 'fixture_runner.py'
    source.write_text(source.read_text() + '\n# changed runner\n')
    service.local_agent.setup({'enabled': True})
    task, _, _ = start(harness, task)
    service.local_agent.stop_event.clear(); service.local_agent.pump()
    saved = service.require(task['id'])
    assert not calls.exists() and saved['jobs'][-1]['status'] == 'failed'
    assert not unresolved(saved) and saved['approval']['attempts_used'] == 0


def test_cancel_before_enable_never_dispatches(harness, tmp_path):
    calls = configure(harness[1], tmp_path, enabled=False)
    task, _, _ = start(harness, approved(harness))
    harness[0].post('/api/tasks/' + task['id'] + '/cancel', json={})
    harness[0].post('/api/local-agent', json={'enabled': True})
    harness[1].local_agent.pump()
    assert not calls.exists() and harness[1].require(task['id'])['jobs'][-1]['status'] == 'cancelled'


def test_native_status_checks_account_and_preserves_local_config(harness, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.app import local_agent
    program = tmp_path / 'codex'; program.write_text('#!/bin/sh\nexit 0\n'); program.chmod(0o700)
    script = Path(__file__).resolve().parents[2] / 'scripts/codex_image_runner.py'
    argv = [sys.executable, str(script), '--codex', str(program)]
    atomic_json(harness[1].local_agent.path, {'mode': 'codex-native', 'enabled': True, 'argv': argv})
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[-1] == '--version':
            return SimpleNamespace(stdout='codex-cli 0.166.0', returncode=0)
        assert command == argv + ['--probe'] and kwargs['shell'] is False
        return SimpleNamespace(stdout=json.dumps({'available': True, 'account_type': 'chatgpt', 'model_turns': 0}), returncode=0)
    monkeypatch.setattr(local_agent.subprocess, 'run', run)
    state = harness[1].local_agent.status()
    assert state['mode'] == 'codex-native' and state['enabled'] is True
    assert not any(key in json.dumps(state) for key in [str(program), str(script)])
    harness[1].local_agent.status()
    assert len(calls) == 2


def test_automatic_result_with_wrong_approved_aspect_is_not_imported(harness, tmp_path):
    service = harness[1]; service.local_agent.stop()
    configure(service, tmp_path)
    task, job, _ = start(harness, approved(harness))
    body = identity(task, job)
    service.claim_agent_image(task['id'], body, local=True)
    response = harness[0].post('/api/tasks/' + task['id'] + '/agent-image-result', data=body,
        files={'image': ('wrong-aspect.png', png(), 'image/png')})
    assert response.status_code == 400
    assert service.require(task['id'])['jobs'][-1]['submissions'][0]['state'] == 'submitted'
    assert not service.require(task['id'])['assets']


def test_new_durable_tool_free_native_turn_is_terminal_without_replaying(harness, tmp_path):
    service = harness[1]; service.local_agent.stop()
    calls = configure(service, tmp_path)
    task, job, payload = start(harness, approved(harness))
    body = identity(task, job)
    claimed = service.claim_agent_image(task['id'], body, local=True, mode='codex-native')
    work = service.local_agent.request_directory(claimed['request_id']); work.mkdir(parents=True)
    prompt = service.require(task['id'])['jobs'][-1]['prompts'][body['slot_id']]['text']
    sha = hashlib.sha256(prompt.encode()).hexdigest()
    atomic_json(work / 'runner-status.json', {'status': 'not_started', 'code': 'native_image_tool_unavailable',
        'request_id': claimed['request_id'], 'prompt_sha256': sha})
    atomic_json(work / 'execution-evidence.json', {'turn_status': 'completed', 'request_id': claimed['request_id'],
        'prompt_sha256': sha, 'image_generation_count': 0, 'raw_image_events': 0,
        'raw_function_calls': 0, 'unknown_tool_events': 0})
    restarted = LocalAgentBridge(service); restarted.pump(); restarted.pump()
    saved = service.require(task['id'])
    assert saved['jobs'][-1]['submissions'][0]['state'] == 'not_started'
    assert saved['jobs'][-1]['status'] == 'failed' and saved['approval']['attempts_used'] == 0
    assert not unresolved(saved) and not calls.exists()
    assert harness[0].post('/api/tasks/' + task['id'] + '/generate', json=payload).json()['job_id'] == job['id']
