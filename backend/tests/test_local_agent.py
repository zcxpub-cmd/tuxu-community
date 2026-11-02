"""Automation contract tests. Images below are fixtures, never real AI evidence."""
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.app.local_agent import LocalAgentBridge, atomic_json
from backend.app.main import unresolved
from backend.tests.test_cloud_service import harness, approved, start, identity, png
from backend.tests.test_service import docs_double
from backend.tests.test_agent_adapter import command_config, local_http


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


def configure_adapter(service, tmp_path, code=None):
    path, script = command_config(tmp_path, code)
    runner = Path(__file__).resolve().parents[2] / 'scripts' / 'local_image_runner.py'
    atomic_json(service.local_agent.path, {'mode': 'local-runner', 'enabled': True, 'label': '我的本机助手',
        'adapter_config': str(path), 'argv': [sys.executable, str(runner), '--config', str(path)], 'timeout_seconds': 1})
    return path, script


def test_adapter_availability_no_invocation_then_automatic_import(harness, tmp_path):
    path, script = configure_adapter(harness[1], tmp_path)
    for _ in range(2):
        state = harness[0].get('/api/bootstrap').json()['models']['local_agent']
        assert state['available'] and state['label'] == '我的本机助手'
        assert str(path) not in json.dumps(state)
    assert not script.with_suffix('.calls').exists()
    task, _, _ = start(harness, approved(harness))
    result = finish_automatic(harness, task['id'])
    assert len(result['assets']) == 1 and result['assets'][0]['source'] == 'local-agent'
    assert result['assets'][0]['model'] == 'local-agent-runner'
    assert result['jobs'][-1]['message'] == '本机智能体图片已保存。'
    harness[1].local_agent.pump()
    assert script.with_suffix('.calls').read_text() == 'x'


def test_http_adapter_automatic_bridge_receives_frozen_request_and_imports(harness, tmp_path, local_http):
    endpoint, observed, _ = local_http
    path = tmp_path / 'http.json'
    atomic_json(path, {'kind': 'http', 'endpoint': endpoint, 'label': '本机HTTP助手'})
    runner = Path(__file__).resolve().parents[2] / 'scripts' / 'local_image_runner.py'
    atomic_json(harness[1].local_agent.path, {'mode': 'local-runner', 'enabled': True,
        'argv': [sys.executable, str(runner), '--config', str(path)], 'adapter_config': str(path), 'timeout_seconds': 3})
    task, _, _ = start(harness, approved(harness))
    result = finish_automatic(harness, task['id'])
    assert len(observed) == 1 and len(result['assets']) == 1
    job = harness[1].require(task['id'])['jobs'][-1]
    assert observed[0][1]['prompt'] == job['prompts'][job['slots'][0]['id']]['text']
    assert 'document' not in observed[0][1]
    harness[1].recover(); LocalAgentBridge(harness[1]).pump()
    assert len(observed) == 1


@pytest.mark.parametrize('dependency', ['config', 'script', 'declared_file'])
def test_inner_adapter_dependency_change_invalidates_unclaimed_approval(harness, tmp_path, dependency):
    service = harness[1]; service.local_agent.stop()
    path, script = configure_adapter(service, tmp_path)
    declared = tmp_path / 'agent-version.txt'; declared.write_text('first')
    body = json.loads(path.read_text()); body['revision_files'] = [str(declared)]; atomic_json(path, body)
    task = approved(harness)
    before = service.local_agent.revision()
    target = path if dependency == 'config' else script if dependency == 'script' else declared
    stat = target.stat()
    data = target.read_text()
    replacement = data.replace('green', 'black') if dependency == 'script' else data.replace('first', 'other') if dependency == 'declared_file' else data.replace('prompt', 'none  ', 1)
    # Configuration replacement keeps valid JSON/schema; size+mtime preservation also tests inner script hashing.
    if dependency == 'config':
        value = json.loads(data); value['label'] = '另一个本机助手'; replacement = json.dumps(value, ensure_ascii=False)
    target.write_text(replacement); __import__('os').utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert service.local_agent.revision() != before
    task, _, _ = start(harness, task)
    service.local_agent.stop_event.clear(); service.local_agent.pump()
    current = service.require(task['id'])
    assert current['jobs'][-1]['status'] == 'failed'
    assert '配置已改变' in current['jobs'][-1]['message']
    assert not script.with_suffix('.calls').exists()


def test_adapter_timeout_kills_process_group_and_restart_never_dispatches_again(harness, tmp_path):
    import os
    if os.name != 'posix': pytest.skip('POSIX process-group lifecycle test')
    service = harness[1]; service.local_agent.stop()
    code = '''import subprocess,sys,time
from pathlib import Path
calls=Path(__file__).with_suffix('.calls'); calls.write_text('x')
subprocess.Popen([sys.executable,'-c',"import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('descendant-ready').touch();time.sleep(2);Path('descendant-survived').touch()"])
time.sleep(10)
'''
    _, script = configure_adapter(service, tmp_path, code)
    task, _, _ = start(harness, approved(harness))
    service.local_agent.stop_event.clear(); service.local_agent.pump()
    current = service.require(task['id']); item = current['jobs'][-1]['submissions'][0]
    assert item['bridge_state'] == 'unknown' and script.with_suffix('.calls').read_text() == 'x'
    assert (service.local_agent.request_directory(item['request_id']) / 'descendant-ready').exists()
    restarted = LocalAgentBridge(service); restarted.pump(); restarted.pump()
    assert script.with_suffix('.calls').read_text() == 'x'
    time.sleep(1.4)
    assert not (service.local_agent.request_directory(item['request_id']) / 'descendant-survived').exists()


@pytest.mark.parametrize('change', ['live_configuration', 'program_file'])
def test_adapter_claim_uses_frozen_configuration_and_rechecks_programs(harness, tmp_path, monkeypatch, change):
    service = harness[1]; service.local_agent.stop()
    path, old = configure_adapter(service, tmp_path)
    original_claim = service.claim_agent_image
    other = tmp_path / 'other_agent.py'; other.write_text(old.read_text())
    def claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        if change == 'live_configuration':
            value = json.loads(path.read_text()); value['argv'][1] = str(other); atomic_json(path, value)
        else:
            stat = old.stat(); old.write_text(old.read_text().replace('green', 'black'))
            __import__('os').utime(old, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return result
    monkeypatch.setattr(service, 'claim_agent_image', claim)
    task, _, _ = start(harness, approved(harness))
    service.local_agent.stop_event.clear(); service.local_agent.pump()
    current = service.require(task['id'])
    assert not other.with_suffix('.calls').exists(), 'A new command cannot run under the old approval'
    if change == 'live_configuration':
        assert old.with_suffix('.calls').read_text() == 'x' and len(current['assets']) == 1
    else:
        assert not old.with_suffix('.calls').exists() and not current['assets']
        assert current['jobs'][-1]['submissions'][0]['bridge_state'] == 'unknown'


def _synthetic_claimed_receipts(harness, tmp_path, slots=1):
    """Synthetic protocol receipts only; no native model or external API is called."""
    import copy
    import uuid
    from backend.tests.test_service import ready

    client, service, *_ = harness
    service.local_agent.stop()
    calls = configure(service, tmp_path, enabled=False)
    task = ready(harness)
    if slots > 1:
        plan = copy.deepcopy(task['plan'])
        for _ in range(slots - 1):
            slot = copy.deepcopy(plan['slots'][0])
            slot['id'] = str(uuid.uuid4())
            plan['slots'].append(slot)
        response = client.put('/api/tasks/' + task['id'] + '/plan', json={
            'version': plan['version'], 'style': plan['style'], 'slots': plan['slots']})
        assert response.status_code == 200
        task = response.json()
    response = client.post('/api/tasks/' + task['id'] + '/approve', json={
        'version': task['plan']['version'], 'confirm': True, 'image_provider': 'codex', 'max_attempts': 3})
    assert response.status_code == 200
    task, job, _ = start(harness, response.json())
    receipts = []
    for slot in job['slots']:
        body = {'job_id': job['id'], 'slot_id': slot['id'], 'approval_id': job['approval_id']}
        claimed = service.claim_agent_image(task['id'], body, local=True, mode='codex-native')
        work = service.local_agent.request_directory(claimed['request_id'])
        work.mkdir(parents=True)
        frozen = service.require(task['id'])['jobs'][-1]['prompts'][slot['id']]['text']
        digest = hashlib.sha256(frozen.encode()).hexdigest()
        atomic_json(work / 'request.json', {'protocol': 'tuxu-local-image-v1', 'request_id': claimed['request_id'],
            'approval_id': body['approval_id'], 'prompt': frozen, 'prompt_sha256': digest})
        Image.new('RGB', (96, 96), 'red').save(work / 'image.png')
        atomic_json(work / 'result.json', {'status': 'completed', 'mode': 'codex-native',
            'request_id': claimed['request_id'], 'prompt_sha256': digest, 'image': 'image.png'})
        atomic_json(work / 'execution-evidence.json', {'fixture_only': True})
        receipts.append((body, claimed, work))
    return task, job, receipts, calls


@pytest.mark.parametrize('status', [400, 422])
@pytest.mark.parametrize('detail', ['返回图片像素尺寸不正确。', '返回图片与批准的画幅比例不符。', '返回内容不是可读取的图片。'])
def test_validated_receipt_known_image_rejection_is_terminal_without_refund_or_replay(harness, tmp_path, monkeypatch, status, detail):
    from backend.app.local_agent import REJECTED
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path)
    service = harness[1]
    before = service.require(task['id'])
    invocations = []
    def reject(*args):
        invocations.append(1)
        raise HTTPException(status, detail)
    monkeypatch.setattr(service, 'complete_agent_image', reject)
    bridge = LocalAgentBridge(service)
    bridge.pump()
    # Re-enable the configured fixture runner after terminal rejection: restart
    # must still never dispatch the already generated request again.
    config = json.loads(service.local_agent.path.read_text())
    config['enabled'] = True
    atomic_json(service.local_agent.path, config)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    current = saved['jobs'][-1]
    assert current['status'] == saved['status'] == 'failed'
    assert current['submissions'][0]['state'] == 'failed'
    assert current['submissions'][0]['bridge_state'] == 'rejected'
    assert current['submissions'][0]['rejection'] == current['error'] == REJECTED
    assert current['attempts'] == before['jobs'][-1]['attempts']
    assert saved['approval']['attempts_used'] == before['approval']['attempts_used']
    assert not unresolved(saved) and not saved['assets']
    assert invocations == [1] and not calls.exists()
    shown = harness[0].get('/api/tasks/' + task['id']).json()
    assert detail not in json.dumps(shown) and shown['jobs'][-1]['submissions'][0]['bridge_state'] == 'rejected'


@pytest.mark.parametrize('status,detail', [
    (400, 'PRIVATE_UNKNOWN_DETAIL'), (422, 'PRIVATE_UNKNOWN_DETAIL'),
    (403, '返回图片与批准的画幅比例不符。'),
    (409, '返回图片与批准的画幅比例不符。'),
    (500, '返回图片像素尺寸不正确。'),
])
def test_unattributed_http_failure_keeps_original_unknown_claim(harness, tmp_path, monkeypatch, status, detail):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path)
    service = harness[1]
    def reject(*args):
        raise HTTPException(status, detail)
    monkeypatch.setattr(service, 'complete_agent_image', reject)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    current = saved['jobs'][-1]
    assert current['status'] == 'awaiting_agent' and unresolved(saved)
    assert current['submissions'][0]['state'] == 'submitted'
    assert current['submissions'][0]['bridge_state'] == 'unknown'
    assert 'rejection' not in current['submissions'][0] and not calls.exists()
    assert detail not in json.dumps(harness[0].get('/api/tasks/' + task['id']).json())


@pytest.mark.parametrize('field', ['receipt_request_id', 'receipt_hash', 'request_request_id', 'request_approval', 'request_prompt'])
def test_changed_receipt_identity_never_becomes_definitive_rejection(harness, tmp_path, monkeypatch, field):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path)
    _body, _claim, work = receipts[0]
    path = work / ('result.json' if field.startswith('receipt_') else 'request.json')
    record = json.loads(path.read_text())
    key = {'receipt_request_id': 'request_id', 'receipt_hash': 'prompt_sha256',
        'request_request_id': 'request_id', 'request_approval': 'approval_id', 'request_prompt': 'prompt'}[field]
    record[key] = 'tampered-synthetic-value'
    atomic_json(path, record)
    def forbidden(*args):
        raise AssertionError('identity must be checked before completion')
    monkeypatch.setattr(harness[1], 'complete_agent_image', forbidden)
    LocalAgentBridge(harness[1]).pump()
    current = harness[1].require(task['id'])['jobs'][-1]
    assert current['status'] == 'awaiting_agent'
    assert current['submissions'][0]['state'] == 'submitted'
    assert current['submissions'][0]['bridge_state'] == 'unknown' and not calls.exists()


def test_rejection_does_not_terminate_changed_approval_during_completion(harness, tmp_path, monkeypatch):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path)
    service = harness[1]
    def change_then_reject(*args):
        def change(current):
            current['approval']['id'] = 'new-synthetic-approval'
            current['status'] = 'approved'
        service.update(task['id'], change)
        raise HTTPException(400, '返回图片与批准的画幅比例不符。')
    monkeypatch.setattr(service, 'complete_agent_image', change_then_reject)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    assert saved['approval']['id'] == 'new-synthetic-approval' and saved['status'] == 'approved'
    assert saved['jobs'][-1]['status'] == 'awaiting_agent'
    assert saved['jobs'][-1]['submissions'][0]['state'] == 'submitted'
    assert saved['jobs'][-1]['submissions'][0]['bridge_state'] == 'unknown' and not calls.exists()


@pytest.mark.parametrize('rejected_index', [0, 1])
def test_one_rejected_receipt_preserves_other_valid_image_and_aggregates_failed(harness, tmp_path, monkeypatch, rejected_index):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path, slots=2)
    service = harness[1]
    before = service.require(task['id'])
    original = service.complete_agent_image
    rejected_slot = receipts[rejected_index][0]['slot_id']
    def complete(task_id, body, contents):
        if body['slot_id'] == rejected_slot:
            raise HTTPException(400, '返回图片与批准的画幅比例不符。')
        return original(task_id, body, contents)
    monkeypatch.setattr(service, 'complete_agent_image', complete)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    current = saved['jobs'][-1]
    assert current['status'] == saved['status'] == 'failed'
    assert [item['state'] for item in current['submissions']].count('saved') == 1
    assert [item['bridge_state'] for item in current['submissions']].count('rejected') == 1
    assert len(saved['assets']) == 1 and not unresolved(saved)
    assert current['attempts'] == before['jobs'][-1]['attempts']
    assert saved['approval']['attempts_used'] == before['approval']['attempts_used']
    asset = saved['assets'][0]
    response = harness[0].post('/api/tasks/' + task['id'] + '/select', json={
        'selections': {asset['slot_id']: {'asset_id': asset['id'], 'use_processed': False}}})
    assert response.status_code == 200, response.text
    LocalAgentBridge(service).pump()
    assert not calls.exists()


def test_rejected_receipt_keeps_other_unresolved_slot_pending(harness, tmp_path, monkeypatch):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path, slots=2)
    service = harness[1]
    reject_slot = receipts[0][0]['slot_id']
    def complete(task_id, body, contents):
        if body['slot_id'] == reject_slot:
            raise HTTPException(400, '返回图片像素尺寸不正确。')
        raise RuntimeError('PRIVATE_UNCERTAIN')
    monkeypatch.setattr(service, 'complete_agent_image', complete)
    LocalAgentBridge(service).pump()
    current = service.require(task['id'])['jobs'][-1]
    assert current['status'] == 'awaiting_agent'
    assert [item['state'] for item in current['submissions']] == ['failed', 'submitted']
    assert [item['bridge_state'] for item in current['submissions']] == ['rejected', 'unknown']
    assert not calls.exists()


@pytest.mark.parametrize('pending_state', ['submitting', 'future_unresolved'])
def test_rejection_never_terminalizes_another_unrecognized_or_submitting_claim(harness, tmp_path, monkeypatch, pending_state):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path, slots=2)
    service = harness[1]
    def pending(current):
        current['jobs'][-1]['submissions'][1]['state'] = pending_state
    service.update(task['id'], pending)
    def reject(*args):
        raise HTTPException(400, '返回图片像素尺寸不正确。')
    monkeypatch.setattr(service, 'complete_agent_image', reject)
    LocalAgentBridge(service).pump()
    current = service.require(task['id'])['jobs'][-1]
    assert current['status'] == 'awaiting_agent'
    assert [item['state'] for item in current['submissions']] == ['failed', pending_state]
    assert not calls.exists()


def test_cancel_requested_before_definitive_receipt_rejection_remains_cancelled(harness, tmp_path, monkeypatch):
    task, job, receipts, calls = _synthetic_claimed_receipts(harness, tmp_path)
    service = harness[1]
    service.update(task['id'], lambda current: current.update(cancel_requested=True))
    def reject(*args):
        raise HTTPException(400, '返回图片像素尺寸不正确。')
    monkeypatch.setattr(service, 'complete_agent_image', reject)
    LocalAgentBridge(service).pump()
    saved = service.require(task['id'])
    assert saved['status'] == saved['jobs'][-1]['status'] == 'cancelled'
    assert saved['jobs'][-1]['submissions'][0]['bridge_state'] == 'rejected'
    assert saved['error'] is None and not calls.exists()
