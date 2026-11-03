"""Local-only role/config/review tests with isolated Stores and synthetic pixels."""
import base64
import copy
import hashlib
import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, PngImagePlugin

from backend.app.builtin_agent import BuiltinAgentRuntime, LIMITS, ROLES, SUGGESTIONS, public_review
from backend.app.director import default_director
from backend.app.store import Store
from backend.app.vision_review import (CATEGORIES, ReviewInterrupted, ReviewUnavailable, VisionReviewAdapter,
                                       image_input, local_ollama_url, parse_checks)


def checks(verdict='match'):
    return [{'category': category, 'verdict': verdict} for category in CATEGORIES]


class FakeVision:
    def __init__(self, verdict='match'):
        self.verdict = verdict
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.result = None
        self.error = None

    def review(self, model, brief, encoded_image, *, before_call=None):
        if before_call:
            before_call()
        self.calls.append({'model': model, 'brief': brief, 'image': encoded_image})
        self.entered.set()
        assert self.release.wait(5), 'test did not release synthetic model'
        if self.error:
            raise self.error
        return self.result or {'checks': checks(self.verdict), 'evidence': {'model_calls': 1, 'calls': [{'eval_count': 12}]}}


class FakeText:
    def analyze(self, *args):
        return {'args': args, 'read_chunks': [{'model_evidence': {'eval_count': 3}}]}

    def prompts(self, *args):
        return {'args': args, 'private_prompt': 'SYNTHETIC_PRIVATE_PROMPT'}


class MiniService:
    def __init__(self, directory):
        self.store = Store(directory)
        self.model_lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.ollama = FakeText()

    def update(self, task_id, change):
        return self.store.mutate(task_id, change)

    def require(self, task_id):
        return self.store.get(task_id)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    # main.current_asset is imported lazily by the runner. Guard its module-level
    # application construction before any possible import as well as this Store.
    monkeypatch.setenv('TUXU_DATA_DIR', str(tmp_path / 'module-isolation'))
    service, vision = MiniService(tmp_path / 'data'), FakeVision()
    runtime = BuiltinAgentRuntime(service, vision)
    yield service, runtime, vision
    vision.release.set()
    service.executor.shutdown(wait=True)


def task_asset(service, runtime, *, automated=False, enabled=False):
    if enabled:
        config = runtime.catalog()
        runtime.configure({'model': 'configured-model', 'visual_review_enabled': True,
                           'expected_revision': config['revision']})
    from backend.app.main import digest, prompt_hash, snapshot
    task_id, asset_id, approval_id = (str(uuid.uuid4()) for _ in range(3))
    plan = {'version': 1, 'model': 'plan-model', 'style': 'simple', 'slots': [
        {'id': 'slot-1', 'description': '一片黄色银杏叶，木桌上。', 'director': default_director()}]}
    task = {'id': task_id, 'document': {'source_hash': 'synthetic-document-hash'}, 'plan': plan,
            'prompts': {'slot-1': {'text': 'SYNTHETIC_PRIVATE_PROMPT_MUST_NOT_REACH_VISION', 'negative': 'private-negative'}},
            'approval': {'id': approval_id, 'document_hash': 'synthetic-document-hash', 'plan_version': 1,
                         'plan_hash': digest(plan), 'checkpoint': 'synthetic-image-model'},
            'cancel_requested': False, '_prompting': False, 'jobs': [], 'status': 'review', 'assets': []}
    task['approval']['prompt_hash'] = prompt_hash(task)
    if automated:
        task['approval']['_builtin_agent'] = runtime.snapshot('plan-model')
        task['automation'] = {'id': str(uuid.uuid4()), 'status': 'running', 'stage': 'visual_review'}
    directory = service.store.directory / 'tasks' / task_id / 'assets'
    directory.mkdir(parents=True)
    source = directory / (asset_id + '.png')
    Image.new('RGB', (96, 64), 'yellow').save(source)
    asset = {'id': asset_id, 'slot_id': 'slot-1', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
             'stale': False, **snapshot(task)}
    task['assets'].append(asset)
    service.store.put(task)
    return task_id, asset_id, source


def until(service, task_id, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = service.require(task_id)
        if condition(task):
            return task
        time.sleep(.005)
    raise AssertionError('synthetic review did not reach the expected state')


def terminal(service, task_id):
    return until(service, task_id, lambda task: task['assets'][0].get('_builtin_visual_review', {}).get('status')
                 not in (None, 'queued', 'running'))


def test_catalog_read_is_nonmutating_and_uses_fixed_contract(harness):
    service, runtime, _ = harness
    before = service.store.database.read_bytes()
    result = runtime.catalog()
    assert result['runtime'] == 'tuxu-builtin'
    assert result['visual_review_enabled'] is False
    assert result['roles'] == list(ROLES) and result['limits'] == LIMITS
    assert service.store.database.read_bytes() == before
    with service.store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM settings WHERE key=?', ('builtin_agent_config',)).fetchone()[0] == 0


def test_configure_cas_and_aba_and_snapshot_does_not_migrate_plan(harness):
    _, runtime, _ = harness
    initial = runtime.catalog()
    changed = runtime.configure({'model': 'qwen2.5vl:7b', 'visual_review_enabled': True,
                                 'expected_revision': initial['revision']})
    assert changed['revision'] != initial['revision']
    assert runtime.snapshot('previous-plan-model')['model'] == 'previous-plan-model'
    assert runtime.catalog()['model'] == 'qwen2.5vl:7b'
    same = runtime.configure({'model': 'qwen2.5vl:7b', 'visual_review_enabled': True,
                              'expected_revision': changed['revision']})
    assert same == changed
    with pytest.raises(HTTPException) as error:
        runtime.configure({'model': 'other-model', 'visual_review_enabled': False,
                           'expected_revision': initial['revision']})
    assert error.value.status_code == 409
    reverted = runtime.configure({'model': initial['model'], 'visual_review_enabled': False,
                                  'expected_revision': changed['revision']})
    assert reverted['revision'] not in (initial['revision'], changed['revision'])


@pytest.mark.parametrize('patch', [
    {'api_key': 'synthetic-value'}, {'endpoint': 'https://example.invalid'}, {'model': 'https://example.invalid'},
    {'model': 'a\nb'}, {'model': '--arbitrary-command'}, {'model': 'sk-synthetic-credential'},
    {'visual_review_enabled': 1}, {'visual_review_enabled': 'true'}, {'expected_revision': 'invalid'}])
def test_configuration_rejects_unexpected_or_secret_bearing_fields(harness, patch):
    _, runtime, _ = harness
    before = runtime.catalog()
    body = {'model': 'plan-model', 'visual_review_enabled': True, 'expected_revision': before['revision'], **patch}
    with pytest.raises(HTTPException):
        runtime.configure(body)
    assert runtime.catalog() == before


def test_role_wrappers_preserve_existing_local_outputs(harness):
    service, runtime, vision = harness
    document = {'paragraphs': []}
    result = runtime.analyze(document, 'report', 'style', 'local-model')
    assert result['args'] == (document, 'report', 'style', 'local-model')
    assert result['read_chunks'][0]['model_evidence']['eval_count'] == 3
    assert runtime.prompts([], 'style', 'local-model')['private_prompt'] == 'SYNTHETIC_PRIVATE_PROMPT'
    assert vision.calls == []


@pytest.mark.parametrize('verdict,status', [('match', 'passed'), ('mismatch', 'needs_attention'), ('uncertain', 'needs_attention')])
def test_review_uses_plan_model_only_visible_brief_and_actual_pixels(harness, verdict, status):
    service, runtime, vision = harness
    vision.verdict = verdict
    task_id, asset_id, source = task_asset(service, runtime)
    original = source.read_bytes()
    before_approval = copy.deepcopy(service.require(task_id)['approval'])
    runtime.request_review(task_id, asset_id, confirm=True)
    task = terminal(service, task_id)
    record = task['assets'][0]['_builtin_visual_review']
    assert record['status'] == status
    assert vision.calls[0]['model'] == 'plan-model'
    assert set(vision.calls[0]['brief']) == {'description', 'director'}
    assert 'SYNTHETIC_PRIVATE_PROMPT' not in json.dumps(vision.calls)
    with Image.open(io.BytesIO(base64.b64decode(vision.calls[0]['image']))) as image:
        assert image.size == (96, 64)
        assert image.getpixel((0, 0)) == (255, 255, 0)
    assert source.read_bytes() == original and task['approval'] == before_approval
    assert record['evidence']['image']['original_sha256'] == hashlib.sha256(original).hexdigest()
    assert runtime.summary(task)['status'] == status
    runtime.request_review(task_id, asset_id, confirm=True)
    assert len(vision.calls) == 1


def test_automatic_review_requires_new_enabled_frozen_profile(harness):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime, automated=True, enabled=True)
    runtime.request_review(task_id, asset_id, automated=True)
    assert terminal(service, task_id)['assets'][0]['_builtin_visual_review']['status'] == 'passed'
    assert len(vision.calls) == 1


def test_disabled_frozen_profile_or_manual_call_during_automation_cannot_start(harness):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime, automated=True)
    for automated in (True, False):
        with pytest.raises(HTTPException) as error:
            runtime.request_review(task_id, asset_id, automated=automated)
        assert error.value.status_code == 409
    assert vision.calls == []


@pytest.mark.parametrize('change', ['cancel', 'signal', 'config', 'plan', 'approval', 'source', 'restart'])
def test_late_results_cannot_publish_after_scope_or_original_changes(harness, change):
    service, runtime, vision = harness
    task_id, asset_id, source = task_asset(service, runtime)
    vision.release.clear()
    runtime.request_review(task_id, asset_id)
    assert vision.entered.wait(3)
    if change == 'cancel':
        service.update(task_id, lambda task: task.update(cancel_requested=True))
    elif change == 'signal':
        runtime.signal_cancel(task_id)
    elif change == 'config':
        config = runtime.catalog()
        runtime.configure({'model': 'changed-global-model', 'visual_review_enabled': True,
                           'expected_revision': config['revision']})
    elif change == 'plan':
        service.update(task_id, lambda task: task['plan']['slots'][0].update(description='另一种主体'))
    elif change == 'approval':
        service.update(task_id, lambda task: task['approval'].update(id=str(uuid.uuid4())))
    elif change == 'source':
        Image.new('RGB', (96, 64), 'blue').save(source)
    elif change == 'restart':
        runtime.recover()
    vision.release.set()
    task = terminal(service, task_id)
    assert task['assets'][0]['_builtin_visual_review']['status'] == 'interrupted'
    service.executor.submit(lambda: None).result(timeout=3)
    assert task['assets'][0]['_builtin_visual_review']['checks'] == []
    assert len(vision.calls) == 1


def test_queued_cancel_and_restart_never_submit_a_model_request(harness):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime)
    with service.model_lock:
        runtime.request_review(task_id, asset_id)
        assert runtime.busy(service.require(task_id))
        runtime.recover()
    service.executor.submit(lambda: None).result(timeout=3)
    task = service.require(task_id)
    assert task['assets'][0]['_builtin_visual_review']['status'] == 'interrupted'
    runtime.request_review(task_id, asset_id)
    service.executor.submit(lambda: None).result(timeout=3)
    assert vision.calls == []


def test_cancellation_signal_during_final_original_read_blocks_publication(harness, monkeypatch):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime)
    captured = runtime._captured_image
    reads = []
    def read(*args):
        result = captured(*args)
        reads.append(True)
        if len(reads) == 3:
            # This runs inside _terminal's Store transaction. The HTTP cancel
            # route signals before it can acquire this same Store lock.
            runtime.signal_cancel(task_id)
        return result
    monkeypatch.setattr(runtime, '_captured_image', read)
    runtime.request_review(task_id, asset_id)
    record = terminal(service, task_id)['assets'][0]['_builtin_visual_review']
    assert len(vision.calls) == 1 and len(reads) == 3
    assert record['status'] == 'interrupted' and record['checks'] == []


def test_signal_cancel_while_queued_prevents_initial_model_request(harness):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime)
    with service.model_lock:
        runtime.request_review(task_id, asset_id)
        runtime.signal_cancel(task_id)
        service.update(task_id, lambda task: task.update(cancel_requested=True))
    assert terminal(service, task_id)['assets'][0]['_builtin_visual_review']['status'] == 'interrupted'
    assert vision.calls == []


def test_changed_config_cannot_create_second_review_under_same_approval(harness):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime)
    runtime.request_review(task_id, asset_id)
    terminal(service, task_id)
    config = runtime.catalog()
    runtime.configure({'model': 'new-model', 'visual_review_enabled': True, 'expected_revision': config['revision']})
    task = runtime.request_review(task_id, asset_id)
    assert task['assets'][0]['_builtin_visual_review']['status'] == 'interrupted'
    service.executor.submit(lambda: None).result(timeout=3)
    assert len(vision.calls) == 1


def test_same_pixels_in_different_slots_require_independent_review(harness):
    service, runtime, vision = harness
    first_id, first_asset, first_source = task_asset(service, runtime)
    second_id, second_asset, second_source = task_asset(service, runtime)
    assert first_source.read_bytes() == second_source.read_bytes()
    runtime.request_review(first_id, first_asset)
    terminal(service, first_id)
    runtime.request_review(second_id, second_asset)
    terminal(service, second_id)
    assert len(vision.calls) == 2
    assert service.require(first_id)['assets'][0]['_builtin_visual_review']['binding'] != service.require(second_id)['assets'][0]['_builtin_visual_review']['binding']


def test_same_task_same_pixels_different_slots_have_distinct_semantic_bindings(harness):
    service, runtime, vision = harness
    task_id, asset_id, source = task_asset(service, runtime)
    from backend.app.main import digest, prompt_hash, snapshot
    second_id = str(uuid.uuid4())
    second_source = source.with_name(second_id + '.png')
    second_source.write_bytes(source.read_bytes())
    def another_slot(task):
        task['plan']['slots'].append({**copy.deepcopy(task['plan']['slots'][0]), 'id': 'slot-2', 'description': '两片蓝色叶片。'})
        task['prompts']['slot-2'] = {'text': 'SYNTHETIC_SECOND_PRIVATE_PROMPT', 'negative': ''}
        task['approval'].update(plan_hash=digest(task['plan']), prompt_hash=prompt_hash(task))
        task['assets'][0].update(snapshot(task))
        task['assets'].append({**copy.deepcopy(task['assets'][0]), 'id': second_id, 'slot_id': 'slot-2'})
    service.update(task_id, another_slot)
    runtime.request_review(task_id, asset_id)
    terminal(service, task_id)
    runtime.request_review(task_id, second_id)
    until(service, task_id, lambda task: task['assets'][1].get('_builtin_visual_review', {}).get('status') == 'passed')
    task = service.require(task_id)
    records = [asset['_builtin_visual_review'] for asset in task['assets']]
    assert len(vision.calls) == 2
    assert records[0]['binding']['asset_sha256'] == records[1]['binding']['asset_sha256']
    assert records[0]['binding']['brief_hash'] != records[1]['binding']['brief_hash']
    assert records[0]['binding']['slot_id'] != records[1]['binding']['slot_id']
    assert runtime.summary(task) == {'status': 'passed', 'total_images': 2, 'completed_images': 2}


def test_original_hash_mismatch_cannot_reach_model(harness):
    service, runtime, vision = harness
    task_id, asset_id, source = task_asset(service, runtime)
    Image.new('RGB', (96, 64), 'red').save(source)
    runtime.request_review(task_id, asset_id)
    assert terminal(service, task_id)['assets'][0]['_builtin_visual_review']['status'] == 'interrupted'
    assert vision.calls == []


def test_invalid_or_injected_result_never_becomes_passed_or_exposes_prose(harness):
    service, runtime, vision = harness
    task_id, asset_id, _ = task_asset(service, runtime)
    vision.result = {'checks': [], 'status': 'passed', 'suggestions': ['SYNTHETIC_PRIVATE_PROMPT']}
    runtime.request_review(task_id, asset_id)
    task = terminal(service, task_id)
    assert task['assets'][0]['_builtin_visual_review']['status'] == 'unavailable'
    public = public_review(task['assets'][0]['_builtin_visual_review'])
    assert public['suggestions'] == [SUGGESTIONS['unavailable']]
    assert 'SYNTHETIC_PRIVATE_PROMPT' not in json.dumps(public)


@pytest.mark.parametrize('url', ['https://example.com', 'http://example.com', 'http://127.0.0.1.evil.invalid',
                               'http://user:password@127.0.0.1:11434', 'http://127.0.0.1/private',
                               'http://127.0.0.1:11434/?api_key=synthetic', 'http://127.0.0.1:0'])
def test_vision_rejects_external_or_credential_bearing_endpoints(url):
    with pytest.raises(ReviewUnavailable):
        local_ollama_url(url)


@pytest.mark.parametrize('value', [{'checks': []}, {'checks': checks()[:-1]}, {'checks': checks() + checks()[:1]},
                                 {'checks': checks(), 'status': 'passed'},
                                 {'checks': [{'category': 'subject', 'verdict': 'match'}] * 5},
                                 {'checks': [{'category': category, 'verdict': 'arbitrary text'} for category in CATEGORIES]}])
def test_review_schema_is_complete_unique_and_enum_only(value):
    with pytest.raises(ReviewUnavailable):
        parse_checks(value)


def test_public_projection_validates_checks_and_drops_raw_fields():
    source = {'status': 'passed', 'checks': checks('uncertain'), 'raw': 'SYNTHETIC_PRIVATE_PROMPT',
              'binding': {'model': 'local-model', 'private': 'SYNTHETIC_PRIVATE_PROMPT'},
              'suggestions': ['SYNTHETIC_PRIVATE_PROMPT']}
    public = public_review(source)
    assert public['status'] == 'needs_attention'
    assert set(public) == {'status', 'checks', 'suggestions', 'model'}
    assert public['suggestions'] == [SUGGESTIONS[category] for category in CATEGORIES]
    assert 'SYNTHETIC_PRIVATE_PROMPT' not in json.dumps(public)
    assert public_review({**source, 'checks': []})['status'] == 'unavailable'


def test_model_input_removes_metadata_and_thumbnail_does_not_rewrite_original():
    info = PngImagePlugin.PngInfo()
    info.add_text('prompt', 'SYNTHETIC_PRIVATE_PROMPT')
    stream = io.BytesIO()
    Image.new('RGB', (1600, 900), 'yellow').save(stream, format='PNG', pnginfo=info)
    original = stream.getvalue()
    encoded, evidence = image_input(original, hashlib.sha256(original).hexdigest())
    decoded = base64.b64decode(encoded)
    assert b'SYNTHETIC_PRIVATE_PROMPT' not in decoded
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.size == (1280, 720)
    assert evidence['original_sha256'] == hashlib.sha256(original).hexdigest()
    assert evidence['image_input_sha256'] == hashlib.sha256(decoded).hexdigest()
    with pytest.raises(ReviewInterrupted):
        image_input(original, '0' * 64)


def mock_ollama(monkeypatch, handler):
    import backend.app.vision_review as module
    client = httpx.Client
    def local_client(**kwargs):
        assert kwargs['trust_env'] is False
        return client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, 'Client', local_client)


def ollama_response(content, **overrides):
    return {'done': True, 'done_reason': 'stop', 'message': {'content': content}, 'eval_count': 20, **overrides}


def test_actual_adapter_sends_image_and_only_repairs_invalid_format_once(monkeypatch):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        result = {'checks': []} if len(calls) == 1 else {'checks': checks()}
        return httpx.Response(200, json=ollama_response(json.dumps(result)))
    mock_ollama(monkeypatch, handler)
    result = VisionReviewAdapter().review('local-model', {'description': 'visible'}, 'BASE64_PIXELS')
    assert len(calls) == 2 and result['evidence']['model_calls'] == 2
    assert result['checks'] == checks()
    assert all(call['messages'][1]['images'] == ['BASE64_PIXELS'] for call in calls)
    assert all(call['messages'][1]['content'] == '{"description": "visible"}' for call in calls)
    assert 'checks' not in calls[1]['messages'][1]['content']
    assert all(call['options'] == {'temperature': 0, 'num_predict': 1000, 'num_ctx': 8192} for call in calls)


@pytest.mark.parametrize('failure', ['timeout', 'http', 'incomplete', 'length', 'twice_invalid'])
def test_unknown_or_failed_vision_call_is_not_blindly_retried(monkeypatch, failure):
    calls = []
    def handler(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('SYNTHETIC_PRIVATE_ERROR')
        if failure == 'http':
            return httpx.Response(500, text='SYNTHETIC_PRIVATE_ERROR')
        response = ollama_response('{"checks": []}')
        if failure == 'incomplete':
            response['done'] = False
        if failure == 'length':
            response['done_reason'] = 'length'
        return httpx.Response(200, json=response)
    mock_ollama(monkeypatch, handler)
    with pytest.raises(ReviewUnavailable) as error:
        VisionReviewAdapter().review('local-model', {'description': 'visible'}, 'BASE64_PIXELS')
    assert len(calls) == (2 if failure == 'twice_invalid' else 1)
    assert 'SYNTHETIC_PRIVATE_ERROR' not in str(error.value)


def test_cancellation_between_format_attempts_prevents_second_model_call(monkeypatch):
    calls, guards = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=ollama_response('{"checks": []}'))
    def guard():
        guards.append(True)
        if len(guards) > 1:
            raise ReviewInterrupted('cancelled')
    mock_ollama(monkeypatch, handler)
    with pytest.raises(ReviewInterrupted):
        VisionReviewAdapter().review('local-model', {'description': 'visible'}, 'BASE64_PIXELS', before_call=guard)
    assert len(calls) == 1
