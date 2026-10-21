"""Service contract tests: fake local models, isolated SQLite, no model inference."""
import hashlib
import json
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.main import create_app


class FakeText:
    invalid = None
    block = None
    calls = 0
    prompt_calls = 0

    def health(self):
        return {'available': True, 'model': 'fake-text', 'error': None}

    def analyze(self, document, genre, style, model):
        self.calls += 1
        if self.block:
            self.block.wait(5)
        ids = [p['id'] for p in document['paragraphs']]
        coverage = ids[:-1] if self.invalid == 'missing' else ids
        if self.invalid == 'unknown':
            coverage = ids[:-1] + ['invented-id']
        if self.invalid == 'duplicate':
            coverage = ids + ids[:1]
        return {'summary': '全文分析', 'coverage': coverage, 'slots': [
            {'paragraph_id': ids[0], 'description': '一棵树的科普示意插画', 'reason': '解释原文',
             'caption': '树木示意图', 'risk': '仅示意', 'aspect_ratio': 'square'}]}

    def prompts(self, slots, style, model):
        self.prompt_calls += 1
        result = {s['id']: {'text': 'A tree illustration, consistent editorial style, no text.', 'negative': 'watermark, text'} for s in slots}
        for slot in slots:
            objects = slot.get('director', {}).get('objects', [])
            if objects:
                result[slot['id']]['regions'] = [{'object_id': item['id'], 'text': 'One approved tree object with green leaves.'} for item in objects]
                result[slot['id']]['background_text'] = 'A neutral background and simple support surface.'
        return result


class FakeComfy:
    def __init__(self):
        self.calls = 0
        self.uncertain = False
        self.findable = True
        self.requests = {}
        self.block = None
        self.entered = threading.Event()
        self.region_available = True
        self.mask_available = True
        self.last_regions = None
        self.last_prompt = None
        self.last_mode = None
        self.reject = False

    def health(self):
        return {'available': True, 'checkpoints': ['fake-checkpoint'], 'error': None,
                'region_control': {'available': self.region_available, 'error': None if self.region_available else 'missing nodes'},
                'mask_control': {'available': self.mask_available, 'error': None if self.mask_available else 'missing mask nodes'}}

    def submit(self, checkpoint, prompt, negative, seed, aspect, submission_key, regions=None, conditioning_mode=None):
        self.calls += 1
        self.last_regions = regions
        self.last_prompt = prompt
        self.last_mode = conditioning_mode
        if self.reject:
            from backend.app.models import RejectedSubmissionError
            raise RejectedSubmissionError('本地 ComfyUI 校验拒绝，未入队：区域节点不可用。')
        request_id = 'request-' + str(self.calls)
        self.requests[submission_key] = request_id
        if self.uncertain:
            raise TimeoutError('unknown submit response')
        return request_id

    def find_submission(self, submission_key):
        return self.requests.get(submission_key) if self.findable else None

    def result(self, request_id, destination):
        self.entered.set()
        if self.block:
            self.block.wait(5)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 64), (32, 80, 120)).save(destination)
        return destination


@pytest.fixture
def docs_double(monkeypatch):
    """Isolate API delegation; document preservation has its own real-format tests."""
    module = types.ModuleType('backend.app.documents')
    def parse_text(text, title):
        return {'id': 'doc-id', 'title': title, 'format': 'txt', 'source_hash': hashlib.sha256(text.encode()).hexdigest(),
                'paragraphs': [{'id': 'p' + str(i), 'text': p, 'kind': 'paragraph', 'level': 0, 'source_index': i}
                               for i, p in enumerate(text.split('\n\n')) if p.strip()],
                'warnings': [], 'character_count': len(text), 'source_filename': 'pasted.txt'}
    def parse_document(path):
        return parse_text(path.read_text(), path.stem)
    def export_document(source_path, document, plan, selections, output_dir, format):
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = {'docx': '.docx', 'markdown': '.zip', 'html': '.html'}[format]
        output = output_dir / ('illustrated' + suffix)
        output.write_text(json.dumps({'text': [p['text'] for p in document['paragraphs']],
                                      'captions': [s['caption'] for s in selections]}, ensure_ascii=False))
        return output
    module.parse_text, module.parse_document, module.export_document = parse_text, parse_document, export_document
    monkeypatch.setitem(sys.modules, 'backend.app.documents', module)
    return module


@pytest.fixture
def harness(tmp_path, docs_double):
    text, comfy = FakeText(), FakeComfy()
    app = create_app(tmp_path, text, comfy)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        token = client.get('/api/bootstrap').json()['token']
        client.headers.update({'X-Local-Token': token})
        yield client, app.state.service, text, comfy
        if comfy.block:
            comfy.block.set()
        if text.block:
            text.block.set()
        app.state.service.executor.shutdown(wait=True)


def wait(client, task_id, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = client.get('/api/tasks/' + task_id).json()
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        if job['status'] not in ('queued', 'running'):
            return task, job
        time.sleep(.01)
    raise AssertionError('fake task did not finish')


def ready(harness):
    client, _, _, _ = harness
    task = client.post('/api/tasks/paste', json={'title': '测试文章', 'text': '树木帮助理解生态。\n\n这是第二段原始内容。', 'genre': 'report', 'style': '统一科普插画'}).json()
    response = client.post(f'/api/tasks/{task["id"]}/analyze', json={'model': 'fake-text'})
    assert response.status_code == 200, response.text
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'completed'
    return task


def approve(client, task):
    response = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True})
    assert response.status_code == 200, response.text
    return response.json()


def generated(harness):
    client = harness[0]
    task = approve(client, ready(harness))
    payload = {'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'generation-1'}
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    assert response.status_code == 200, response.text
    task, job = wait(client, task['id'], response.json()['job_id'])
    return task, job, payload


def test_same_origin_token_and_host_boundary(harness):
    client = harness[0]
    assert client.post('/api/tasks/paste', headers={'X-Local-Token': ''}, json={}).status_code == 403
    assert client.get('/api/bootstrap', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/bootstrap', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/bootstrap', headers={'Origin': 'http://127.0.0.1:8765'}).status_code == 200


def test_persistence_and_private_path_not_exposed(harness):
    client, service, _, _ = harness
    task = ready(harness)
    assert 'source_path' not in task
    from backend.app.store import Store
    reloaded = Store(service.store.directory)
    assert reloaded.get(task['id'])['plan'] == task['plan']
    assert reloaded.token == service.store.token
    assert client.get('/api/tasks/missing').status_code == 404
    assert client.post('/api/tasks/missing/analyze', json={}).status_code == 404


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'unknown'])
def test_analysis_requires_exact_full_coverage(harness, failure):
    client, _, text, _ = harness
    text.invalid = failure
    task = client.post('/api/tasks/paste', json={'text': '第一段\n\n第二段'}).json()
    response = client.post(f'/api/tasks/{task["id"]}/analyze', json={}).json()
    task, job = wait(client, task['id'], response['job_id'])
    assert job['status'] == 'failed'
    assert task['plan'] is None
    assert '覆盖' in task['error']


def test_unauthorized_generation_and_stale_version_are_blocked(harness):
    client = harness[0]
    task = ready(harness)
    assert client.post(f'/api/tasks/{task["id"]}/generate', json={'version': 1, 'approval_id': 'fake', 'idempotency_key': 'x'}).status_code == 409
    task = approve(client, task)
    assert client.post(f'/api/tasks/{task["id"]}/generate', json={'version': 999, 'approval_id': task['approval']['id'], 'idempotency_key': 'x'}).status_code == 409
    assert harness[3].calls == 0


def test_generation_idempotent_assets_fingerprint_and_skip(harness):
    client, _, _, comfy = harness
    task, job, payload = generated(harness)
    assert job['status'] == 'completed'
    assert len(task['assets']) == 1
    asset = task['assets'][0]
    assert asset['approval_id'] == task['approval']['id']
    assert asset['document_hash'] == task['document']['source_hash']
    assert asset['plan_hash'] and asset['prompt_hash'] and asset['checkpoint'] == 'fake-checkpoint'
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload).json()
    assert response['job_id'] == job['id']
    assert comfy.calls == 1
    payload['idempotency_key'] = 'generation-2'
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload).json()
    task, job = wait(client, task['id'], response['job_id'])
    assert job['status'] == 'completed' and comfy.calls == 1
    assert client.get(asset['url']).headers['content-type'] == 'image/png'


def test_plan_change_invalidates_prompts_assets_and_selection(harness):
    client = harness[0]
    task, _, _ = generated(harness)
    asset = task['assets'][0]
    selection = {asset['slot_id']: {'asset_id': asset['id'], 'use_processed': False}}
    assert client.post(f'/api/tasks/{task["id"]}/select', json={'selections': selection}).status_code == 200
    response = client.put(f'/api/tasks/{task["id"]}/plan', json={'version': task['plan']['version'], 'style': '新的统一风格', 'slots': task['plan']['slots']})
    assert response.status_code == 200
    task = response.json()
    assert task['approval'] is None and harness[1].require(task['id'])['prompts'] == {} and task['selections'] == {}
    assert task['assets'][0]['stale'] is True
    task = approve(client, task)
    assert client.post(f'/api/tasks/{task["id"]}/select', json={'selections': selection}).status_code == 409


def test_public_prompt_editor_is_disabled_and_keeps_internal_approved_text(harness):
    client, service, text, _ = harness
    task = approve(client, ready(harness))
    before = service.require(task['id'])
    prompts = before['prompts']
    first = next(iter(prompts))
    prompts[first]['text'] = 'A user-approved green tree, editorial illustration, no text.'
    assert client.put(f'/api/tasks/{task["id"]}/prompts', json={'prompts': prompts}).status_code == 410
    assert client.put(f'/api/tasks/{task["id"]}/prompts', json={'plan_version': task['plan']['version'], 'prompts': prompts}).status_code == 410
    assert service.require(task['id'])['approval'] == before['approval']
    assert service.require(task['id'])['prompts'][first]['text'] != prompts[first]['text']
    assert 'prompts' not in task
    assert text.prompt_calls == 1


def director_ready(harness):
    from backend.app.director import default_director
    client = harness[0]
    task = ready(harness)
    slot = task['plan']['slots'][0]
    slot['director'] = {**default_director(), 'style_id': 'watercolor', 'brightness': 'low',
        'camera': 'topdown', 'lighting': 'backlit', 'objects': [
            {'id': 'tree', 'label': '树木', 'description': '绿色叶片', 'x': 12, 'y': 20,
             'width': 28, 'height': 50, 'depth': 'foreground'}]}
    response = client.put(f'/api/tasks/{task["id"]}/plan', json={
        'version': task['plan']['version'], 'style': task['plan']['style'], 'slots': [slot]})
    assert response.status_code == 200, response.text
    return response.json()


def test_director_choices_generate_visible_prompts_and_real_region_payload(harness):
    client, service, _, comfy = harness
    task = approve(client, director_ready(harness))
    prompt = service.require(task['id'])['prompts'][task['plan']['slots'][0]['id']]
    assert 'watercolor' in prompt['text'] and 'dark' in prompt['text']
    assert 'overhead view' in prompt['text'] and 'back light' in prompt['text']
    assert prompt['regions'][0]['object_id'] == 'tree'
    assert 'foreground' in prompt['regions'][0]['text']
    assert prompt['regions'][0]['text'].startswith('One approved tree object with green leaves.\n')
    assert 'middle-left' in prompt['regions'][0]['text']
    assert 'watercolor' in prompt['background_text']
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'directed'})
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'completed' and comfy.calls == 1
    area = comfy.last_regions[0]
    assert area['x'] == 120 and area['y'] == 200 and area['width'] == 296 and area['height'] == 520
    asset = task['assets'][0]
    assert asset['director'] == task['plan']['slots'][0]['director']
    private_asset = service.require(task['id'])['assets'][0]
    assert private_asset['regions'] == prompt['regions'] and private_asset['region_areas'] == comfy.last_regions
    assert comfy.last_prompt == private_asset['prompt'] == private_asset['global_text'] == prompt['global_text']
    assert private_asset['scene_prompt'] == prompt['text'] and private_asset['background_text'] == prompt['background_text']
    assert private_asset['global_text'] == private_asset['background_text'] != private_asset['scene_prompt']
    assert comfy.last_mode == private_asset['conditioning_mode'] == prompt['conditioning_mode'] == 'masked_regions_v1'
    from backend.app.models import ComfyAdapter
    graph = ComfyAdapter.workflow('fake-checkpoint', private_asset['prompt'], private_asset['negative_prompt'],
                                  private_asset['seed'], task['plan']['slots'][0]['aspect_ratio'], comfy.last_regions, comfy.last_mode)
    assert graph['2']['inputs']['text'] == service.require(task['id'])['jobs'][-1]['prompts'][asset['slot_id']]['global_text']
    assert graph['13']['inputs']['text'] == prompt['regions'][0]['text']
    assert 'scene_prompt' not in asset and 'background_text' not in asset and 'global_text' not in asset and 'prompt' not in asset and 'conditioning_mode' not in asset
    assert 'regions' not in asset and all('text' not in area for area in asset['region_areas'])
    assert service.require(task['id'])['jobs'][-1]['slots'][0]['director'] == asset['director']


def test_new_regional_approval_retranslates_missing_background_and_rejects_incomplete_conversion(harness):
    from backend.app.main import prompt_hash
    client, service, text, _ = harness
    task = approve(client, director_ready(harness))
    def legacy(t):
        for prompt in t['prompts'].values():
            prompt.pop('background_text')
        t['approval']['prompt_hash'] = prompt_hash(t)
    service.store.mutate(task['id'], legacy)
    task = approve(client, client.get('/api/tasks/' + task['id']).json())
    assert text.prompt_calls == 2
    assert all('background_text' in p for p in service.require(task['id'])['prompts'].values())

    service.store.mutate(task['id'], legacy)
    real_fake_prompts = text.prompts
    def incomplete(slots, style, model):
        values = real_fake_prompts(slots, style, model)
        for prompt in values.values():
            prompt.pop('background_text')
        return values
    text.prompts = incomplete
    response = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True})
    assert response.status_code == 422
    current = service.require(task['id'])
    assert current['approval'] is None and not current['_prompting'] and text.prompt_calls == 3


def test_new_regional_reapproval_translates_again_instead_of_reusing_old_shared_regions(harness):
    client, service, text, _ = harness
    task = approve(client, director_ready(harness))
    before = service.require(task['id'])
    task = approve(client, task)
    after = service.require(task['id'])
    assert text.prompt_calls == 2 and after['approval']['id'] != before['approval']['id']
    assert after['approval']['attempts_used'] == 0


def test_isolated_object_failure_does_not_save_shared_prompt_or_authorization(harness):
    from backend.app.models import ModelError, OllamaAdapter
    client, service, _, comfy = harness
    task = director_ready(harness)
    slot_id = task['plan']['slots'][0]['id']
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.calls = getattr(self, 'calls', 0) + 1
            if 'object' in payload:
                raise ModelError('独立对象转换失败，不保存共享区域正文。')
            return {'prompts': {slot_id: {'text': 'One tree on neutral ground.', 'negative': 'text',
                'background_text': 'Neutral ground.', 'regions': [{'object_id': 'tree', 'text': 'Shared tree draft.'}]}}}
    adapter = Adapter()
    service.ollama = adapter
    response = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True})
    assert response.status_code == 422 and comfy.calls == 0 and adapter.calls == 2
    saved = service.require(task['id'])
    assert saved['prompts'] == {} and saved['approval'] is None and saved['_prompting'] is False
    assert saved['status'] == 'plan_ready' and [item['phase'] for item in adapter.last_prompt_evidence] == ['scene', 'object']
    assert adapter.last_prompt_evidence[-1]['request_state'] == 'failed'


@pytest.mark.parametrize('repair_valid', [False, True])
@pytest.mark.parametrize('excluded_field', ['text', 'background_text'])
def test_scene_exclusion_repair_is_bounded_and_authorizes_only_complete_valid_result(harness, repair_valid, excluded_field):
    from backend.app.models import OllamaAdapter
    client, service, _, comfy = harness
    task = director_ready(harness)
    slot_id = task['plan']['slots'][0]['id']
    class Adapter(OllamaAdapter):
        scene_calls = 0
        calls = 0
        def _chat(self, model, system, payload, output_format='json'):
            self.calls += 1
            if 'object' in payload:
                return {'object_id': 'tree', 'text': 'One green tree.', '_model_evidence': {'done': True, 'eval_count': 7}}
            self.scene_calls += 1
            self.scene_payloads = getattr(self, 'scene_payloads', []) + [json.loads(json.dumps(payload))]
            value = {'text': 'One tree on neutral ground.', 'negative': 'writing, watermark',
                     'background_text': 'Neutral ground.', 'regions': []}
            if self.scene_calls == 1 or not repair_valid:
                value[excluded_field] = 'A neutral scene without text. PRIVATE_REJECTED_DRAFT'
            return {'prompts': {slot_id: value}, '_model_evidence': {'done': True, 'eval_count': 11}}
    adapter = Adapter()
    service.ollama = adapter
    response = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True})
    saved = service.require(task['id'])
    assert comfy.calls == 0 and adapter.scene_calls == 2
    assert adapter.scene_payloads[0] == adapter.scene_payloads[1]
    assert 'PRIVATE_REJECTED_DRAFT' not in response.text
    if repair_valid:
        assert response.status_code == 200 and adapter.calls == 3
        assert saved['status'] == 'approved' and saved['approval'] is not None
        assert [item['phase'] for item in saved['approval']['prompt_evidence']] == ['scene', 'scene_repair', 'object']
        assert 'PRIVATE_REJECTED_DRAFT' not in json.dumps(saved['prompts'])
        assert saved['prompts'][slot_id]['negative'] == 'writing, watermark'
        assert 'prompt_evidence' not in response.json()['approval']
    else:
        assert response.status_code == 422 and adapter.calls == 2
        assert saved['status'] == 'plan_ready' and saved['prompts'] == {} and saved['approval'] is None
        assert saved['_prompting'] is False
        assert response.json()['detail'] == '画面描述转换未完成，请重试或调整简单描述。'


@pytest.mark.parametrize('source_authorized', [False, True])
def test_known_text_clause_routing_authorizes_only_explicit_source_and_never_calls_comfy(harness, source_authorized):
    from backend.app.models import DEFAULT_STYLE, OllamaAdapter
    client, service, _, comfy = harness
    task = director_ready(harness)
    if source_authorized:
        response = client.put(f'/api/tasks/{task["id"]}/plan', json={
            'version': task['plan']['version'], 'style': DEFAULT_STYLE, 'slots': task['plan']['slots']})
        assert response.status_code == 200
        task = response.json()
    slot_id = task['plan']['slots'][0]['id']
    class Adapter(OllamaAdapter):
        calls = 0
        def _chat(self, model, system, payload, output_format='json'):
            self.calls += 1
            if 'object' in payload:
                return {'object_id': 'tree', 'text': 'One green tree.'}
            return {'prompts': {slot_id: {'text': 'One green tree. No text.',
                'background_text': 'White ground.', 'negative': 'watermark, distortion', 'regions': []}}}
    adapter = Adapter()
    service.ollama = adapter
    response = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True})
    saved = service.require(task['id'])
    assert comfy.calls == 0 and adapter.calls == 2
    if source_authorized:
        assert response.status_code == 200 and saved['approval'] is not None
        prompt = saved['prompts'][slot_id]
        assert 'No text' not in prompt['text'] and 'No text' not in prompt['global_text']
        assert prompt['negative'] == 'watermark, distortion, text'
        evidence = saved['approval']['prompt_evidence']
        assert [item['phase'] for item in evidence] == ['scene', 'object']
        assert evidence[0]['text_exclusion_normalization'] == {'fields': ['text'], 'negative_nouns': ['text'], 'source_explicit': True}
        assert 'prompt_evidence' not in response.json()['approval']
    else:
        assert response.status_code == 422 and saved['approval'] is None and saved['prompts'] == {}
        assert saved['status'] == 'plan_ready' and saved['_prompting'] is False
        assert [item['phase'] for item in adapter.last_prompt_evidence] == ['scene', 'scene_repair']


@pytest.mark.parametrize('background_only', [False, True])
def test_legacy_regional_unknown_job_recovers_original_global_text_without_recompile_or_resubmit(harness, background_only):
    from backend.app.main import prompt_hash
    client, service, text, comfy = harness
    task = approve(client, director_ready(harness))
    def legacy(t):
        for prompt in t['prompts'].values():
            prompt.pop('global_text')
            prompt.pop('conditioning_mode')
            if background_only:
                prompt['background_text'] = ' Legacy background input.\nDirector controls: unchanged legacy.'
            else:
                prompt.pop('background_text')
            prompt['regions'][0]['text'] = 'Legacy approved object text.'
        t['approval']['prompt_hash'] = prompt_hash(t)
    service.store.mutate(task['id'], legacy)
    approved_prompts = service.require(task['id'])['prompts']
    saved_prompt = next(iter(approved_prompts.values()))
    global_text = saved_prompt.get('background_text', saved_prompt['text'])
    approval_hash = service.require(task['id'])['approval']['prompt_hash']
    payload = {'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'legacy-region'}
    comfy.uncertain, comfy.findable = True, False
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'failed' and job['submissions'][0]['state'] == 'submitting'
    assert comfy.last_prompt == global_text and comfy.calls == 1
    comfy.uncertain, comfy.findable = False, True
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    task, job = wait(client, task['id'], response.json()['job_id'])
    internal = service.require(task['id'])
    assert job['status'] == 'completed' and comfy.calls == 1 and text.prompt_calls == 1
    assert internal['prompts'] == approved_prompts and internal['approval']['prompt_hash'] == approval_hash
    assert internal['assets'][0]['prompt'] == global_text and 'global_text' not in internal['assets'][0]
    if background_only:
        assert internal['assets'][0]['background_text'] == saved_prompt['background_text']
        assert internal['assets'][0]['scene_prompt'] == saved_prompt['text']
    else:
        assert 'background_text' not in internal['assets'][0] and 'scene_prompt' not in internal['assets'][0]
    assert next(iter(internal['jobs'][-1]['prompts'].values()))['regions'][0]['text'] == 'Legacy approved object text.'


def test_changing_only_private_global_text_invalidates_approval_without_a_model_call(harness):
    client, service, text, comfy = harness
    task = approve(client, director_ready(harness))
    before = service.require(task['id'])
    slot_id = task['plan']['slots'][0]['id']
    service.store.mutate(task['id'], lambda t: t['prompts'][slot_id].update(global_text='A changed global scene.'))
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'changed-global'})
    assert response.status_code == 409 and comfy.calls == 0 and text.prompt_calls == 1
    after = service.require(task['id'])
    assert after['jobs'] == before['jobs'] and after['approval']['attempts_used'] == 0


def test_director_position_change_invalidates_previous_approval_and_assets(harness):
    client = harness[0]
    task = approve(client, director_ready(harness))
    old_approval = task['approval']
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': old_approval['id'], 'idempotency_key': 'old-area'})
    task, _ = wait(client, task['id'], response.json()['job_id'])
    asset = task['assets'][0]
    selection = {asset['slot_id']: {'asset_id': asset['id'], 'use_processed': False}}
    assert client.post(f'/api/tasks/{task["id"]}/select', json={'selections': selection}).status_code == 200
    task['plan']['slots'][0]['director']['objects'][0]['x'] = 50
    task = client.put(f'/api/tasks/{task["id"]}/plan', json={
        'version': task['plan']['version'], 'style': task['plan']['style'], 'slots': task['plan']['slots']}).json()
    assert task['approval'] is None and harness[1].require(task['id'])['prompts'] == {} and task['selections'] == {}
    assert task['assets'][0]['stale'] is True
    task = approve(client, task)
    assert task['approval']['plan_hash'] != old_approval['plan_hash']
    assert client.post(f'/api/tasks/{task["id"]}/select', json={'selections': selection}).status_code == 409


def test_region_prompt_editor_cannot_reveal_or_modify_private_controls(harness):
    client, service, _, _ = harness
    task = approve(client, director_ready(harness))
    old_approval = service.require(task['id'])['approval']
    slot_id = task['plan']['slots'][0]['id']
    private_prompts = service.require(task['id'])['prompts']
    private_prompts[slot_id]['text'] = 'A manually edited scene.'
    private_prompts[slot_id]['regions'][0]['text'] = 'A tree with more detailed green foliage.'
    response = client.put(f'/api/tasks/{task["id"]}/prompts', json={
        'plan_version': task['plan']['version'], 'prompts': private_prompts})
    assert response.status_code == 410, response.text
    assert service.require(task['id'])['approval'] == old_approval
    assert service.require(task['id'])['prompts'] != private_prompts
    assert 'prompts' not in client.get('/api/tasks/' + task['id']).json()
    assert harness[2].prompt_calls == 1


def test_missing_mask_nodes_block_new_approval_and_submission_without_unknown_intent(harness):
    client, _, _, comfy = harness
    task = director_ready(harness)
    comfy.mask_available = False
    assert client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True}).status_code == 409
    comfy.mask_available = True
    task = approve(client, task)
    comfy.mask_available = False
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'missing-nodes'})
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'failed' and '区域控制不可用' in job['error']
    assert job['submissions'] == [] and comfy.calls == 0 and task['approval']['attempts_used'] == 0
    task['plan']['slots'][0]['director']['objects'] = []
    assert client.put(f'/api/tasks/{task["id"]}/plan', json={
        'version': task['plan']['version'], 'slots': task['plan']['slots']}).status_code == 200


def test_new_mask_mode_does_not_require_legacy_area_nodes_and_is_frozen_through_asset(harness):
    client, service, _, comfy = harness
    task = director_ready(harness)
    comfy.region_available = False
    task = approve(client, task)
    prompt = next(iter(service.require(task['id'])['prompts'].values()))
    assert prompt['conditioning_mode'] == 'masked_regions_v1' and prompt['global_text'] == prompt['background_text']
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'mask-no-area'})
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'completed' and comfy.calls == 1 and comfy.last_mode == 'masked_regions_v1'
    internal = service.require(task['id'])
    asset = internal['assets'][0]
    assert asset['conditioning_mode'] == prompt['conditioning_mode']
    assert asset['prompt'] == asset['global_text'] == asset['background_text'] == prompt['background_text']
    assert asset['scene_prompt'] == prompt['text'] and asset['scene_prompt'] != asset['prompt']
    assert internal['jobs'][-1]['prompts'] == internal['prompts']
    for url in ('/api/bootstrap', '/api/tasks', '/api/tasks/' + task['id']):
        assert 'conditioning_mode' not in client.get(url).text


def test_changing_only_private_mode_invalidates_approval_before_any_submit(harness):
    client, service, text, comfy = harness
    task = approve(client, director_ready(harness))
    before = service.require(task['id'])
    slot_id = task['plan']['slots'][0]['id']
    service.store.mutate(task['id'], lambda t: t['prompts'][slot_id].pop('conditioning_mode'))
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'changed-mode'})
    after = service.require(task['id'])
    assert response.status_code == 409 and comfy.calls == 0 and text.prompt_calls == 1
    assert after['jobs'] == before['jobs'] and after['approval']['attempts_used'] == 0


def test_mask_mode_unknown_resume_preserves_original_input_and_never_recompiles(harness, monkeypatch):
    client, service, text, comfy = harness
    task = approve(client, director_ready(harness))
    frozen = service.require(task['id'])['prompts']
    payload = {'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'mask-unknown'}
    comfy.uncertain, comfy.findable = True, False
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'failed' and comfy.calls == 1 and job['submissions'][0]['state'] == 'submitting'
    assert comfy.last_mode == 'masked_regions_v1' and comfy.last_prompt == next(iter(frozen.values()))['background_text']
    job_id = job['id']
    import backend.app.main as main
    monkeypatch.setattr(main, 'compile_controls', lambda *args, **kwargs: pytest.fail('Resume recompiled frozen input'))
    comfy.uncertain, comfy.findable, comfy.mask_available = False, True, False
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    task, job = wait(client, task['id'], response.json()['job_id'])
    internal = service.require(task['id'])
    assert job['status'] == 'completed' and job['id'] == job_id and comfy.calls == 1 and text.prompt_calls == 1
    assert internal['prompts'] == frozen and internal['jobs'][-1]['prompts'] == frozen
    assert internal['assets'][0]['conditioning_mode'] == 'masked_regions_v1'


def test_legacy_area_health_selector_blocks_old_no_mode_job_before_intent(harness):
    from backend.app.main import prompt_hash
    client, service, _, comfy = harness
    task = approve(client, director_ready(harness))
    def legacy(t):
        for value in t['prompts'].values():
            value.pop('conditioning_mode')
        t['approval']['prompt_hash'] = prompt_hash(t)
    service.store.mutate(task['id'], legacy)
    comfy.region_available, comfy.mask_available = False, True
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'legacy-no-area'})
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'failed' and job['submissions'] == [] and comfy.calls == 0
    assert task['approval']['attempts_used'] == 0


def test_positive_comfy_validation_rejection_releases_unknown_lock_and_allows_plan_change(harness):
    client, _, _, comfy = harness
    task = approve(client, director_ready(harness))
    comfy.reject = True
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={
        'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': '400-rejection'})
    task, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'failed' and job['submissions'][0]['state'] == 'failed'
    assert job['submissions'][0]['request_id'] is None and comfy.requests == {}
    assert '未接受请求' in job['error']
    task['plan']['slots'][0]['director']['objects'] = []
    response = client.put(f'/api/tasks/{task["id"]}/plan', json={
        'version': task['plan']['version'], 'slots': task['plan']['slots']})
    assert response.status_code == 200, response.text


def test_reading_legacy_approved_task_does_not_rewrite_fingerprints_or_add_new_fields(harness):
    client, service, _, _ = harness
    task = approve(client, ready(harness))
    before = service.require(task['id'])
    fetched = client.get('/api/tasks/' + task['id']).json()
    client.get('/api/bootstrap')
    assert fetched['plan'] == task['plan'] and fetched['approval'] == task['approval']
    assert 'director' not in fetched['plan']['slots'][0]
    assert 'prompts' not in fetched
    assert all('regions' not in value for value in service.require(task['id'])['prompts'].values())
    assert service.require(task['id']) == before


def test_cancel_submitted_request_saves_result_and_rejects_edits_until_done(harness):
    client, _, _, comfy = harness
    task = approve(client, ready(harness))
    comfy.block = threading.Event()
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'cancel-test'}).json()
    assert comfy.entered.wait(2)
    assert client.post(f'/api/tasks/{task["id"]}/cancel', json={}).status_code == 200
    assert client.put(f'/api/tasks/{task["id"]}/plan', json={'version': task['plan']['version'], 'slots': task['plan']['slots'], 'style': task['style']}).status_code == 409
    comfy.block.set()
    task, job = wait(client, task['id'], response['job_id'])
    assert job['status'] == 'cancelled' and len(task['assets']) == 1 and comfy.calls == 1


def test_uncertain_submission_finds_original_without_resubmit(harness):
    harness[3].uncertain = True
    task, job, _ = generated(harness)
    assert job['status'] == 'completed' and len(task['assets']) == 1
    assert harness[3].calls == 1


def test_unknown_submission_blocks_new_key_and_retry_only_queries(harness):
    client, _, _, comfy = harness
    comfy.uncertain, comfy.findable = True, False
    task, job, payload = generated(harness)
    assert job['status'] == 'failed' and comfy.calls == 1
    new = {**payload, 'idempotency_key': 'different-key'}
    assert client.post(f'/api/tasks/{task["id"]}/generate', json=new).status_code == 409
    assert client.post(f'/api/tasks/{task["id"]}/approve', json={'version': task['plan']['version'],
        'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True}).status_code == 409
    comfy.findable = True
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload).json()
    task, job = wait(client, task['id'], response['job_id'])
    assert job['status'] == 'completed' and comfy.calls == 1 and len(task['assets']) == 1


def test_process_select_and_export_are_version_bound(harness):
    client = harness[0]
    task, _, _ = generated(harness)
    asset = task['assets'][0]
    response = client.post(f'/api/tasks/{task["id"]}/process', json={'asset_id': asset['id'], 'denoise': True, 'upscale': 2})
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['assets'][0]['processing']['algorithm']
    assert max(task['assets'][0]['processed_width'], task['assets'][0]['processed_height']) >= 2048
    selection = {asset['slot_id']: {'asset_id': asset['id'], 'use_processed': True}}
    assert client.post(f'/api/tasks/{task["id"]}/select', json={'selections': selection}).status_code == 200
    payload = {'format': 'html', 'confirm_insert': True, 'idempotency_key': 'export-one'}
    response = client.post(f'/api/tasks/{task["id"]}/export', json=payload)
    assert response.status_code == 200, response.text
    entry = response.json()
    assert client.get(entry['url']).status_code == 200
    assert client.post(f'/api/tasks/{task["id"]}/export', json=payload).json()['url'] == entry['url']
    changed = {**payload, 'format': 'docx'}
    assert client.post(f'/api/tasks/{task["id"]}/export', json=changed).status_code == 409
    assert client.get(f'/api/exports/{task["id"]}/missing.html').status_code == 404


def test_import_limit_format_and_original_hash_guard(harness):
    client, service, _, _ = harness
    assert client.post('/api/tasks/import', files={'file': ('bad.pdf', b'PDF')}).status_code == 400
    assert client.post('/api/tasks/paste', json={'text': 'a' * 10001}).status_code == 413
    response = client.post('/api/tasks/import', files={'file': ('original.txt', '第一段\n\n第二段'.encode())})
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['document']['source_filename'] == 'original.txt'
    source = Path(service.require(task['id'])['source_path'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == task['document']['source_hash']


def test_restart_recovers_running_known_request_without_new_submit(harness, tmp_path):
    client, service, text, comfy = harness
    task, job, _ = generated(harness)
    # Simulate a crash after Comfy submission but before saving the returned asset.
    service.store.mutate(task['id'], lambda t: (t.update(assets=[], status='generating'),
        next(j for j in t['jobs'] if j['id'] == job['id']).update(status='running'),
        next(j for j in t['jobs'] if j['id'] == job['id'])['submissions'][0].update(state='submitted')))
    app2 = create_app(service.store.directory, text, comfy)
    with TestClient(app2, base_url='http://127.0.0.1:8765') as second:
        task, recovered = wait(second, task['id'], job['id'])
        assert recovered['status'] == 'completed' and comfy.calls == 1
        assert len(task['assets']) == 1
    app2.state.service.executor.shutdown(wait=True)


def test_adapter_long_paragraph_chunks_are_complete_and_bounded():
    from backend.app.models import OllamaAdapter
    class ReadingAdapter(OllamaAdapter):
        def __init__(self):
            self.read = []
        def _chat(self, model, system, payload, output_format='json'):
            if 'paragraphs' not in payload:
                return {'summary': '全篇方案', 'selected_candidate_ids': [p['candidate_id'] for p in payload['candidate_previews'][:3]]}
            self.read.extend(payload['paragraphs'])
            first = next(p for p in payload['paragraphs'] if p['kind'] == 'paragraph')
            return {'summary': '片段已读', 'coverage': [p['id'] for p in payload['paragraphs']],
                    'slots': [{'paragraph_id': first['id'], 'description': '树木示意插画', 'reason': '解释内容',
                               'caption': '树木示意图', 'risk': '示意', 'aspect_ratio': 'square'}],
                    '_model_evidence': {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 1500}}
    paragraphs = [{'id': 'long', 'text': '长' * 4501, 'kind': 'paragraph', 'level': 0, 'source_index': 0},
                  {'id': 'last', 'text': '末' * 1999, 'kind': 'paragraph', 'level': 0, 'source_index': 1}]
    adapter = ReadingAdapter()
    result = adapter.analyze({'paragraphs': paragraphs}, 'report', '科普插画', 'fake')
    assert ''.join(p['text'] for p in adapter.read if p['id'] == 'long') == paragraphs[0]['text']
    assert ''.join(p['text'] for p in adapter.read if p['id'] == 'last') == paragraphs[1]['text']
    assert result['coverage'] == ['long', 'last']
    assert result['coverage_evidence']['read_character_count'] == 6500
    assert all(c['character_count'] <= 2000 for c in result['read_chunks'])
    assert len({s['paragraph_id'] for s in result['slots']}) == len(result['slots'])
    assert all(c['model_evidence']['prompt_eval_count'] == 1500 for c in result['read_chunks'])


def test_ollama_length_completion_fails_and_options_release_model(monkeypatch):
    import httpx
    from backend.app.models import OllamaAdapter, ModelError
    original_client = httpx.Client
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'done': True, 'done_reason': 'length', 'message': {'content': '{}'}})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(ModelError, match='截断'):
        OllamaAdapter()._chat('fake', 'system', {'sample': 'input'})
    assert seen[0]['keep_alive'] == 0
    assert seen[0]['options']['num_ctx'] >= 8192


@pytest.mark.parametrize('same_parameters', [True, False])
def test_concurrent_processing_has_consistent_immutable_urls(harness, monkeypatch, same_parameters):
    from backend.app import postprocess
    client, service, _, _ = harness
    task, _, _ = generated(harness)
    asset = task['assets'][0]
    source = service.directory(task['id']) / 'assets' / (asset['id'] + '.png')
    image = Image.new('RGB', (1024, 600))
    image.putdata([(x % 256, (x * 13) % 256, (x * 7) % 256) for x in range(1024 * 600)])
    image.save(source)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    service.store.mutate(task['id'], lambda t: t['assets'][0].update(sha256=original_hash, width=1024, height=600))
    original_process = postprocess.process_image
    calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs)
        return original_process(*args, **kwargs)
    monkeypatch.setattr(postprocess, 'process_image', counted)
    parameters = [{'asset_id': asset['id'], 'denoise': False, 'upscale': 2},
                  {'asset_id': asset['id'], 'denoise': False if same_parameters else True, 'upscale': 2 if same_parameters else 3}]
    barrier = threading.Barrier(2)
    def run(index):
        barrier.wait()
        return client.post(f'/api/tasks/{task["id"]}/process', json=parameters[index])
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(run, range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    urls = []
    for response, params in zip(responses, parameters):
        processed = response.json()['assets'][0]
        metadata = processed['processing']
        assert metadata['denoise'] == params['denoise'] and metadata['upscale'] == params['upscale']
        url = processed['processed_url']
        download = client.get(url)
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == download.headers['X-Content-SHA256']
        from io import BytesIO
        assert Image.open(BytesIO(download.content)).size == (metadata['width'], metadata['height'])
        urls.append(url)
    assert len(calls) == (1 if same_parameters else 2)
    assert (urls[0] == urls[1]) is same_parameters
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert not list(source.parent.glob('*.tmp'))
    assert client.get(f'/api/assets/{task["id"]}/{asset["id"]}/processed?version=unknown').status_code == 404


def test_generation_key_is_visible_after_reload(harness):
    client = harness[0]
    task, job, payload = generated(harness)
    task_job = next(j for j in client.get(f'/api/tasks/{task["id"]}').json()['jobs'] if j['id'] == job['id'])
    bootstrap_task = next(t for t in client.get('/api/bootstrap').json()['tasks'] if t['id'] == task['id'])
    bootstrap_job = next(j for j in bootstrap_task['jobs'] if j['id'] == job['id'])
    assert task_job['idempotency_key'] == payload['idempotency_key'] == bootstrap_job['idempotency_key']


def test_saved_zero_slot_plan_exports_without_model_approval_and_version_conflicts(harness):
    client, _, text, comfy = harness
    task = ready(harness)
    task = client.put(f'/api/tasks/{task["id"]}/plan', json={'version': task['plan']['version'],
                    'style': task['style'], 'slots': []}).json()
    assert task['approval'] is None and task['selections'] == {}
    payload = {'format': 'html', 'confirm_insert': True, 'idempotency_key': 'zero-slot-export'}
    response = client.post(f'/api/tasks/{task["id"]}/export', json=payload)
    assert response.status_code == 200, response.text
    entry = response.json()
    assert entry['approval_id'] is None and entry['checkpoint'] is None
    assert client.get(entry['url']).status_code == 200
    assert text.prompt_calls == 0 and comfy.calls == 0
    assert client.post(f'/api/tasks/{task["id"]}/export', json=payload).json()['url'] == entry['url']
    task = client.put(f'/api/tasks/{task["id"]}/plan', json={'version': task['plan']['version'],
                    'style': '新统一风格', 'slots': []}).json()
    assert client.post(f'/api/tasks/{task["id"]}/export', json=payload).status_code == 409
    response = client.post(f'/api/tasks/{task["id"]}/export', json={**payload, 'idempotency_key': 'zero-slot-new-version'})
    assert response.status_code == 200 and response.json()['url'] != entry['url']


@pytest.mark.parametrize('platform_name', ['darwin', 'linux'])
@pytest.mark.parametrize('use_override', [False, True])
def test_import_persists_outside_desktop_by_default_or_explicit_override(tmp_path, docs_double, monkeypatch, use_override, platform_name):
    fake_home = tmp_path / 'home'
    desktop = fake_home / 'Desktop'
    desktop.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: fake_home))
    monkeypatch.delenv('TUXU_DATA_DIR', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.setattr('backend.app.paths.sys.platform', platform_name)
    if use_override:
        monkeypatch.setenv('TUXU_DATA_DIR', str(tmp_path / 'private-override'))
    app = create_app(ollama_adapter=FakeText(), comfy_adapter=FakeComfy())
    source_bytes = '需要本地保存的测试正文。'.encode()
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        token = client.get('/api/bootstrap').json()['token']
        client.headers['X-Local-Token'] = token
        response = client.post('/api/tasks/import', files={'file': ('private.txt', source_bytes)})
        assert response.status_code == 200, response.text
        stored = app.state.service.require(response.json()['id'])
        source = Path(stored['source_path'])
        assert source.read_bytes() == source_bytes
        assert source.is_relative_to(app.state.service.store.directory)
        assert app.state.service.store.database.is_file()
        assert not source.is_relative_to(desktop)
        assert not list(desktop.iterdir())
        if use_override:
            assert app.state.service.store.directory == tmp_path / 'private-override'
        else:
            expected_base = (fake_home / 'Library' / 'Application Support' if platform_name == 'darwin'
                             else fake_home / '.local' / 'share')
            assert source.is_relative_to(expected_base / 'TuxuCommunity' / 'data')
    app.state.service.executor.shutdown(wait=True)
