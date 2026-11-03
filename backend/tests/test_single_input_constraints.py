"""New-approval exclusions for text-only image engines, using synthetic fixtures."""
import copy
import hashlib
import json

import pytest

from backend.app.models import ModelError, OllamaAdapter
from backend.tests.test_cloud_service import harness, approved, start
from backend.tests.test_service import docs_double, ready, wait


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv('TUXU_DATA_DIR', str(tmp_path / 'module-isolation'))


def simple_slot(description='One synthetic tree.'):
    return {'id': 'synthetic-slot', 'description': description}


def test_exclusions_are_compiled_without_mutating_original_model_outputs_or_other_fields():
    raw = {'synthetic-slot': {'text': 'One synthetic tree.', 'negative': 'blur, watermark, unwanted diagram',
                              'background_text': 'Plain ground.', 'regions': [{'object_id': 'tree', 'text': 'A tree.'}]}}
    before = copy.deepcopy(raw)
    result = OllamaAdapter.single_input_prompts(raw, [simple_slot()], '插画，无文字')
    assert raw == before and result is not raw
    compiled = result['synthetic-slot']
    assert compiled['text'].startswith(raw['synthetic-slot']['text'])
    assert raw['synthetic-slot']['negative'] in compiled['text']
    assert 'as image exclusions' in compiled['text']
    assert 'Do not render visible text' in compiled['text']
    assert 'only in the document caption outside the image' in compiled['text']
    for field in ('negative', 'background_text', 'regions'):
        assert compiled[field] == raw['synthetic-slot'][field]


@pytest.mark.parametrize('negative', ['', ' \n\t'])
def test_empty_negative_keeps_exact_prompt_and_hash(negative):
    from backend.app.main import prompt_hash
    raw = {'synthetic-slot': {'text': 'One synthetic tree.', 'negative': negative}}
    result = OllamaAdapter.single_input_prompts(raw, [simple_slot()], '清晰插画')
    assert result == raw
    assert prompt_hash({'prompts': result}) == prompt_hash({'prompts': raw})


@pytest.mark.parametrize('negative', ['', ' \n\t'])
def test_empty_negative_still_preserves_explicit_source_text_ban_without_a_model_request(negative, monkeypatch):
    def no_model_call(*args, **kwargs):
        raise AssertionError('compiling an already approved source constraint must not call a model')
    monkeypatch.setattr(OllamaAdapter, '_chat', no_model_call)
    raw = {'synthetic-slot': {'text': 'One synthetic tree.', 'negative': negative}}
    result = OllamaAdapter.single_input_prompts(raw, [simple_slot()], '插画，无文字')
    assert result['synthetic-slot']['negative'] == negative
    assert result['synthetic-slot']['text'].startswith(raw['synthetic-slot']['text'])
    assert 'Do not render visible text' in result['synthetic-slot']['text']
    assert 'only in the document caption outside the image' in result['synthetic-slot']['text']
    assert 'Apply the following negative prompt' not in result['synthetic-slot']['text']
    assert raw['synthetic-slot']['text'] == 'One synthetic tree.'


@pytest.mark.parametrize('description', [
    '在图片上添加标题“合成示例”。', '在瓶子上显示标签“ATP”。', '图中需要文字说明。',
    'Please include the title Synthetic in the image.', 'Write labels on the pictured bottles.',
    'The image needs labels for the visible items.'])
def test_explicit_positive_text_request_suppresses_only_the_extra_fixed_ban(description):
    raw = {'synthetic-slot': {'text': 'One synthetic image.', 'negative': 'blur, watermark'}}
    result = OllamaAdapter.single_input_prompts(raw, [simple_slot(description)], '插画，无文字')
    assert OllamaAdapter.explicit_visible_text(simple_slot(description), '插画，无文字')
    assert 'blur, watermark' in result['synthetic-slot']['text']
    assert 'Do not render visible text' not in result['synthetic-slot']['text']


@pytest.mark.parametrize('description', [
    '不要添加文字。', '禁止在图中显示标签。', '不需要标题。', '无需添加文字。', '保留颜色并禁止添加文字。',
    'Do not add text.', 'Never show labels.', 'No need to include a title.', 'Keep the colors and do not add labels.',
    'A scientific illustration describing typography.', '标题与标签的概念示意。'])
def test_negated_requests_and_mere_text_nouns_do_not_count_as_positive_requests(description):
    assert not OllamaAdapter.explicit_visible_text(simple_slot(description), '插画，无文字')
    raw = {'synthetic-slot': {'text': 'One synthetic image.', 'negative': 'blur, watermark'}}
    result = OllamaAdapter.single_input_prompts(raw, [simple_slot(description)], '插画，无文字')
    assert 'Do not render visible text' in result['synthetic-slot']['text']


def test_unapproved_text_ban_is_not_added_and_overflow_fails_without_truncating():
    raw = {'synthetic-slot': {'text': 'One synthetic tree.', 'negative': 'blur, watermark'}}
    result = OllamaAdapter.single_input_prompts(raw, [simple_slot()], '清晰插画')
    assert 'Do not render visible text' not in result['synthetic-slot']['text']
    too_long = {'synthetic-slot': {'text': 'A' * 5500, 'negative': 'B' * 600}}
    before = copy.deepcopy(too_long)
    with pytest.raises(ModelError, match='长度限制'):
        OllamaAdapter.single_input_prompts(too_long, [simple_slot()], '清晰插画')
    assert too_long == before


def model_with_negative(text_model, negative='SYNTHETIC_UNWANTED_DIAGRAM, text, watermark'):
    original = text_model.prompts
    def prompts(*args):
        result = original(*args)
        for value in result.values():
            value['text'] = 'One synthetic tree.'
            value['negative'] = negative
        return result
    text_model.prompts = prompts


@pytest.mark.parametrize('provider', ['ark', 'openai', 'codex', 'local'])
def test_new_approval_freezes_effective_single_input_and_dispatches_exactly_that_payload(harness, monkeypatch, tmp_path, provider):
    client, service, text, comfy = harness
    service.local_agent.stop()
    model_with_negative(text)
    if provider == 'openai':
        service.cloud_config.update('openai', {'enabled': True, 'api_key': 'SYNTHETIC_TEST_CREDENTIAL'})
    if provider == 'codex':
        from backend.tests.test_local_agent import configure
        runner_calls = configure(service, tmp_path)
    task = ready(harness)
    result = client.put(f'/api/tasks/{task["id"]}/plan', json={'version': task['plan']['version'],
                        'style': '清晰插画，无文字', 'slots': task['plan']['slots']})
    assert result.status_code == 200
    task = result.json()
    result = client.post(f'/api/tasks/{task["id"]}/approve', json={'version': task['plan']['version'],
                        'confirm': True, 'image_provider': provider, 'checkpoint': 'fake-checkpoint', 'max_attempts': 1})
    assert result.status_code == 200, result.text
    task = result.json()
    private = service.require(task['id'])
    from backend.app.main import prompt_hash
    frozen_hash = prompt_hash(private)
    slot_id = private['plan']['slots'][0]['id']
    frozen = private['prompts'][slot_id]['text']
    assert frozen_hash == private['approval']['prompt_hash']
    assert text.prompt_calls == 1
    if provider == 'local':
        assert 'SYNTHETIC_UNWANTED_DIAGRAM' not in frozen
    else:
        assert private['prompts'][slot_id]['negative'] in frozen and 'Do not render visible text' in frozen
    assert 'SYNTHETIC_UNWANTED_DIAGRAM' not in result.text
    captured = []
    original_submit = service.cloud.submit
    def submit(target, prompt, key):
        captured.append(prompt)
        return original_submit(target, prompt, key)
    service.cloud.submit = submit
    def forbidden_recompile(*args):
        raise AssertionError('generation/recovery must use the frozen prompt without recompiling')
    monkeypatch.setattr(OllamaAdapter, 'single_input_prompts', forbidden_recompile)
    payload = {'version': task['plan']['version'], 'approval_id': task['approval']['id'], 'idempotency_key': 'frozen-once'}
    response = client.post(f'/api/tasks/{task["id"]}/generate', json=payload)
    assert response.status_code == 200, response.text
    final, job = wait(client, task['id'], response.json()['job_id'])
    if provider == 'codex':
        service.local_agent.stop_event.clear()
        service.local_agent.pump()
        private = service.require(task['id'])
        job = private['jobs'][-1]
        request_id = job['submissions'][0]['request_id']
        request = json.loads((service.local_agent.request_directory(request_id) / 'request.json').read_text())
        assert request['prompt'] == frozen
        assert request['prompt_sha256'] == hashlib.sha256(frozen.encode()).hexdigest()
        assert runner_calls.read_text() == 'x'
    elif provider == 'local':
        assert comfy.last_prompt == frozen
        assert comfy.calls == 1
        assert service.require(task['id'])['jobs'][-1]['prompts'][slot_id]['negative'].startswith('SYNTHETIC_UNWANTED_DIAGRAM')
    else:
        assert captured == [frozen] and service.cloud.calls == 1
    current = service.require(task['id'])
    assert current['approval']['prompt_hash'] == frozen_hash
    assert current['jobs'][-1]['snapshot']['prompt_hash'] == frozen_hash
    assert current['jobs'][-1]['prompts'][slot_id]['text'] == frozen
    assert current['approval']['attempts_used'] == 1


@pytest.mark.parametrize('provider', ['ark', 'codex'])
def test_legacy_approved_payload_is_not_rewritten_on_generation_or_recovery(harness, monkeypatch, provider):
    client, service, text, _ = harness
    service.local_agent.stop()
    task = approved(harness, provider)
    from backend.app.main import prompt_hash
    def legacy(task):
        for value in task['prompts'].values():
            value['text'] = 'LEGACY_FROZEN_SINGLE_INPUT_TEXT'
            value['negative'] = 'LEGACY_SEPARATE_NEGATIVE'
        task['approval']['prompt_hash'] = prompt_hash(task)
    service.update(task['id'], legacy)
    old = copy.deepcopy(service.require(task['id'])['prompts'])
    def forbidden(*args):
        raise AssertionError('old approved payload must not be retroactively compiled')
    monkeypatch.setattr(OllamaAdapter, 'single_input_prompts', forbidden)
    task, job, payload = start(harness, task)
    service.recover()
    private = service.require(task['id'])
    assert private['prompts'] == old and private['jobs'][-1]['prompts'] == old
    assert private['approval']['prompt_hash'] == prompt_hash({'prompts': old})
    assert private['jobs'][-1]['snapshot']['prompt_hash'] == private['approval']['prompt_hash']
    assert text.prompt_calls == 1
    assert private['approval']['attempts_used'] == 1


def test_removing_compiled_exclusions_invalidates_authorization_before_any_image_submission(harness):
    client, service, text, _ = harness
    model_with_negative(text)
    task = approved(harness, 'ark')
    def tamper(task):
        for value in task['prompts'].values():
            value['text'] = 'One synthetic tree.'
    service.update(task['id'], tamper)
    response = client.post(f'/api/tasks/{task["id"]}/generate', json={'version': task['plan']['version'],
                           'approval_id': task['approval']['id'], 'idempotency_key': 'tampered'})
    assert response.status_code == 409 and service.cloud.calls == 0
