"""Approved optional background sources and isolated real request contracts."""
import copy
import hashlib
import json

import pytest

from backend.app.director import MASKED_REGIONS_MODE, compile_controls, default_director, region_areas, validate_director
from backend.app.main import digest, prompt_hash, validate_prompts
from backend.app.models import ComfyAdapter, ModelError, OllamaAdapter
from backend.app.privacy import public_task
from backend.tests.test_service import approve, director_ready, docs_double, harness, wait


PRIVATE_BACKGROUND = 'PRIVATE_BACKGROUND_CANARY plain white tabletop.'


def slot(background='空白白色桌面'):
    config = {**default_director(), 'style_id': 'watercolor', 'brightness': 'high',
        'notes': 'PRIVATE_NOTES', 'objects': [
            {'id': 'cup', 'label': '红杯', 'description': '一个红色杯子', 'x': 8, 'y': 25, 'width': 36, 'height': 55, 'depth': 'midground'},
            {'id': 'book', 'label': '蓝书', 'description': '一本合上的蓝书', 'x': 56, 'y': 38, 'width': 36, 'height': 40, 'depth': 'midground'}]}
    if background is not None:
        config['background'] = background
    return {'id': 's', 'description': 'PRIVATE_SCENE 红杯左、蓝书右', 'aspect_ratio': 'landscape',
            'director': validate_director(config)}


def scene_value(text='One red cup beside one closed blue book.'):
    return {'text': text, 'negative': 'text', 'background_text': 'Shared red cup and blue book background.',
            'regions': [{'object_id': 'cup', 'text': 'Rejected shared region body.'}]}


class RecordingAdapter(OllamaAdapter):
    def __init__(self, background_response=None, scene_failure=False, repair=False):
        super().__init__()
        self.calls = []
        self.background_response = background_response
        self.scene_failure = scene_failure
        self.repair = repair

    def health(self):
        return {'available': True, 'model': 'fake-text', 'error': None}

    def _chat(self, model, system, payload, output_format='json'):
        self.calls.append(copy.deepcopy(payload))
        if 'background' in payload:
            if isinstance(self.background_response, Exception):
                raise self.background_response
            return (copy.deepcopy(self.background_response) if self.background_response is not None else
                    {'slot_id': payload['slot_id'], 'text': PRIVATE_BACKGROUND,
                     '_model_evidence': {'done': True, 'eval_count': 9}})
        if 'object' in payload:
            item = payload['object']
            return {'object_id': item['id'], 'text': 'One approved object.'}
        if self.scene_failure:
            raise ModelError('本地场景提示词转换失败。')
        slot_id = payload['slots'][0]['id']
        scene_calls = sum('slots' in p for p in self.calls)
        value = scene_value('One tree without a roof.' if self.repair and scene_calls == 1 else 'One approved tree.')
        return {'prompts': {slot_id: value}}


def test_optional_background_absence_keeps_legacy_normalization_and_hash_bytes():
    old = slot(None)['director']
    before = copy.deepcopy(old)
    assert 'background' not in default_director() and 'background' not in validate_director(old)
    assert validate_director(old) == before and digest(validate_director(old)) == digest(before)
    supplied = copy.deepcopy(old)
    supplied['background'] = '  白色桌面  '
    assert validate_director(supplied)['background'] == '白色桌面'
    assert digest(validate_director(supplied)) != digest(before)
    supplied['background'] = '白' * 300
    assert len(validate_director(supplied)['background']) == 300


@pytest.mark.parametrize('value', [None, 7, True, [], {}, '白' * 301])
def test_optional_background_type_and_length_are_not_silently_ignored(value):
    config = slot(None)['director']
    config['background'] = value
    with pytest.raises(ValueError):
        validate_director(config)


def test_nonempty_background_requires_objects_but_explicit_blank_remains_optional():
    config = default_director()
    config['background'] = '白色桌面'
    with pytest.raises(ValueError):
        validate_director(config)
    config['background'] = ' \n '
    assert validate_director(config)['background'] == ''
    adapter = RecordingAdapter()
    with pytest.raises(ModelError):
        adapter.prompts([{'id': 's', 'description': '环境', 'director': {**default_director(), 'background': '白色桌面'}}], '', 'fake-text')
    assert adapter.calls == []


@pytest.mark.parametrize('background', [None, '', ' \n '])
def test_absent_or_blank_background_uses_prior_auto_translation_without_extra_call(background):
    approved = slot(background)
    adapter = RecordingAdapter()
    result = adapter.prompts([approved], 'PRIVATE_STYLE', 'fake-text')
    assert result['s']['background_text'] == 'Shared red cup and blue book background.'
    assert len(adapter.calls) == 3 and [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'object', 'object']


def test_actual_background_http_request_has_only_source_and_exact_slot_schema(monkeypatch):
    import httpx
    approved = slot()
    original_client, requests = httpx.Client, []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        payload = json.loads(body['messages'][1]['content'])
        if 'background' in payload:
            response = {'slot_id': 's', 'text': 'A plain white tabletop.'}
        elif 'object' in payload:
            response = {'object_id': payload['object']['id'], 'text': 'One red cup.' if payload['object']['id'] == 'cup' else 'One closed blue book.'}
        else:
            response = {'prompts': {'s': {**scene_value(), 'background_text': '共享错误背景正文'}}}
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'eval_count': 11,
            'message': {'content': json.dumps(response)}})
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=transport, **kwargs))
    adapter = OllamaAdapter()
    raw = adapter.prompts([approved], 'PRIVATE_STYLE', 'fake-text')
    assert len(requests) == 4 and raw['s']['background_text'] == 'A plain white tabletop.'
    sent = requests[1]
    payload = json.loads(sent['messages'][1]['content'])
    assert payload == {'slot_id': 's', 'background': '空白白色桌面'}
    assert not any(secret in sent['messages'][1]['content'] for secret in ['红杯', '蓝书', 'PRIVATE_STYLE', 'PRIVATE_SCENE', 'PRIVATE_NOTES'])
    assert sent['format'] == adapter.background_prompt_schema('s')
    assert sent['format']['additionalProperties'] is False and sent['format']['properties']['slot_id']['enum'] == ['s']
    assert sent['options']['temperature'] == 0 and sent['keep_alive'] == 0 and sent['stream'] is False
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'background', 'object', 'object']
    evidence = adapter.last_prompt_evidence[1]
    expected_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert evidence['input_sha256'] == expected_sha and evidence['result_valid'] and evidence['eval_count'] == 11
    compiled = validate_prompts(compile_controls(raw, [approved], conditioning_mode=MASKED_REGIONS_MODE), [approved], english=True)
    value = compiled['s']
    assert value['global_text'] == value['background_text'] and 'watercolor' in value['global_text']
    assert 'One red cup' not in value['global_text'] and 'One closed blue book' not in value['global_text']
    graph = ComfyAdapter.workflow('checkpoint', value['global_text'], value['negative'], 17, 'landscape',
                                 region_areas(approved, value), conditioning_mode=value['conditioning_mode'])
    assert len(graph) == 22 and graph['2']['inputs']['text'] == value['background_text']
    assert all(n['inputs']['set_cond_area'] == 'default' for n in graph.values() if n['class_type'] == 'ConditioningSetMask')
    assert prompt_hash({'prompts': compiled}) != prompt_hash({'prompts': compile_controls({'s': {**raw['s'], 'background_text': 'Different ground.'}}, [approved], conditioning_mode=MASKED_REGIONS_MODE)})


@pytest.mark.parametrize('response', [
    {'slot_id': 'other', 'text': PRIVATE_BACKGROUND}, {'text': PRIVATE_BACKGROUND},
    {'slot_id': 's', 'text': PRIVATE_BACKGROUND, 'extra': 'secret'},
    {'slot_id': 's', 'text': ''}, {'slot_id': 's', 'text': 7},
    {'slot_id': 's', 'text': '中文背景'}, {'slot_id': 's', 'text': 'x' * 651},
    {'slot_id': 's', 'text': 'White ground without props.'},
    {'slot_id': 's', 'text': 'White ground. No text.'},
])
def test_invalid_background_never_uses_shared_draft_or_retries(response):
    adapter = RecordingAdapter(response)
    with pytest.raises(ModelError):
        adapter.prompts([slot()], 'PRIVATE_STYLE', 'fake-text')
    assert len(adapter.calls) == 2 and [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'background']
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False
    assert PRIVATE_BACKGROUND not in json.dumps(adapter.last_prompt_evidence)


def test_one_scene_repair_keeps_already_translated_background_without_an_extra_background_request():
    adapter = RecordingAdapter(repair=True)
    value = adapter.prompts([slot()], 'PRIVATE_STYLE', 'fake-text')['s']
    assert value['background_text'] == PRIVATE_BACKGROUND
    assert len(adapter.calls) == 5 and sum('background' in payload for payload in adapter.calls) == 1
    assert adapter.calls[0] == adapter.calls[2]
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'background', 'scene_repair', 'object', 'object']


def with_background(harness):
    client = harness[0]
    task = director_ready(harness)
    task['plan']['slots'][0]['director']['background'] = '空白白色桌面'
    response = client.put(f'/api/tasks/{task["id"]}/plan', json={
        'version': task['plan']['version'], 'style': task['plan']['style'], 'slots': task['plan']['slots']})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize('failure', ['invalid', 'transport', 'scene'])
def test_background_approval_failure_is_unapproved_zero_comfy_and_body_free(harness, failure):
    client, service, _, comfy = harness
    task = with_background(harness)
    slot_id = task['plan']['slots'][0]['id']
    response = {'slot_id': slot_id, 'text': 'No ' + PRIVATE_BACKGROUND}
    adapter = RecordingAdapter(ModelError('独立背景提示词请求失败。') if failure == 'transport' else response,
                               scene_failure=failure == 'scene')
    service.ollama = adapter
    result = client.post(f'/api/tasks/{task["id"]}/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint', 'max_attempts': 3, 'confirm': True})
    internal = service.require(task['id'])
    assert result.status_code == 422 and comfy.calls == 0 and internal['approval'] is None and internal['prompts'] == {}
    assert internal['status'] == 'plan_ready' and internal['_prompting'] is False
    expected = 1 if failure == 'scene' else 2
    assert len(adapter.calls) == len(internal['prompt_failure_evidence']) == expected
    assert internal['prompt_failure_evidence'][-1]['result_valid'] is False
    assert all(set(item) <= {'phase', 'request_state', 'result_valid', 'input_sha256', 'error_type'} for item in internal['prompt_failure_evidence'])
    assert PRIVATE_BACKGROUND not in json.dumps(internal)
    for url in (f'/api/tasks/{task["id"]}', '/api/tasks', '/api/bootstrap'):
        public = client.get(url).text
        assert 'prompt_failure_evidence' not in public and PRIVATE_BACKGROUND not in public
    assert 'prompt_failure_evidence' not in result.text


def test_independent_background_is_frozen_for_unknown_resume_and_new_source_invalidates_approval(harness, monkeypatch):
    import backend.app.main as main
    client, service, _, comfy = harness
    task = with_background(harness)
    adapter = RecordingAdapter()
    service.ollama = adapter
    task = approve(client, task)
    frozen = copy.deepcopy(service.require(task['id'])['prompts'])
    source_hash = task['approval']['plan_hash']
    payload = {'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'background-unknown'}
    comfy.uncertain, comfy.findable = True, False
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    _, job = wait(client, task['id'], response.json()['job_id'])
    assert job['status'] == 'failed' and comfy.calls == 1 and len(adapter.calls) == 3
    monkeypatch.setattr(main, 'compile_controls', lambda *args, **kwargs: pytest.fail('Resume recompiled background'))
    comfy.findable = True
    resumed = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    task, job = wait(client, task['id'], resumed.json()['job_id'])
    internal = service.require(task['id'])
    assert job['status'] == 'completed' and comfy.calls == 1 and len(adapter.calls) == 3
    assert internal['prompts'] == internal['jobs'][-1]['prompts'] == frozen
    assert internal['assets'][0]['global_text'] == internal['assets'][0]['prompt'] == next(iter(frozen.values()))['background_text']
    assert PRIVATE_BACKGROUND not in json.dumps(task) and task['plan']['slots'][0]['director']['background'] == '空白白色桌面'
    slots = copy.deepcopy(task['plan']['slots'])
    slots[0]['director']['background'] = '浅灰色桌面'
    changed = client.put(f'/api/tasks/{task["id"]}/plan', json={
        'version': task['plan']['version'], 'style': task['plan']['style'], 'slots': slots})
    assert changed.status_code == 200 and changed.json()['approval'] is None
    assert digest(service.require(task['id'])['plan']) != source_hash
    assert service.require(task['id'])['jobs'][-1]['prompts'] == frozen
    assert client.post(f'/api/tasks/{task["id"]}/generate', json=payload).status_code == 409 and comfy.calls == 1


def test_failed_background_evidence_is_recursively_private_without_mutating_old_tasks():
    evidence = {'phase': 'background', 'raw_body': PRIVATE_BACKGROUND}
    task = {'id': 't', 'prompt_failure_evidence': [evidence],
        'plan': {'slots': [{'id': 's', 'description': '简短画面', 'prompt_failure_evidence': [evidence],
                           'director': {'background': '白色桌面', 'objects': []}}]},
        'approval': {'snapshot': {'prompt_failure_evidence': [evidence]}},
        'jobs': [{'status': 'completed', 'snapshot': {'prompt_failure_evidence': [evidence]}}],
        'assets': [{'processing': {'prompt_failure_evidence': [evidence]}}]}
    before = copy.deepcopy(task)
    public = public_task(task)
    assert PRIVATE_BACKGROUND not in json.dumps(public) and 'prompt_failure_evidence' not in json.dumps(public)
    assert task == before and public['plan']['slots'][0]['director']['background'] == '白色桌面'
