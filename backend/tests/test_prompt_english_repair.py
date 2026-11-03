"""Bounded local language correction; all scenes are authored synthetic fixtures."""
import copy
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from backend.app.builtin_agent import BuiltinAgentRuntime
from backend.app.director import default_director
from backend.app.models import ModelError, OllamaAdapter


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv('TUXU_DATA_DIR', str(tmp_path / 'isolated'))


def slot(regional=False, background=False):
    result = {'id': 'synthetic-slot', 'description': '木桌左边一个红色培养瓶，右边一个蓝色培养瓶，体现ATP合酶相关概念的示意。',
              'aspect_ratio': 'landscape'}
    if regional:
        result['director'] = {**default_director(), 'objects': [
            {'id': 'red-flask', 'label': '红色培养瓶', 'description': '一个红色培养瓶', 'x': 8, 'y': 20, 'width': 30,
             'height': 60, 'depth': 'midground'},
            {'id': 'blue-flask', 'label': '蓝色培养瓶', 'description': '一个蓝色培养瓶', 'x': 60, 'y': 20, 'width': 30,
             'height': 60, 'depth': 'midground'}]}
        if background:
            result['director']['background'] = '浅色木桌及素色墙面'
    return result


def scene_value(regional=False, **overrides):
    value = {'text': 'One red culture flask on the left and one blue culture flask on the right, on a wooden table.',
             'negative': 'text, watermark'}
    if regional:
        value.update(background_text='A wooden table and a plain wall.', regions=[
            {'object_id': 'red-flask', 'text': 'UNTRUSTED_SHARED_REGION_DRAFT'}])
    return {**value, **overrides}


class Scripted(OllamaAdapter):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def _chat(self, model, system, payload, output_format='json'):
        self.calls.append({'model': model, 'system': system, 'payload': copy.deepcopy(payload),
                           'schema': copy.deepcopy(output_format)})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def output(value, identifier='synthetic-slot'):
    return {'prompts': {identifier: value}, '_model_evidence': {'done': True, 'eval_count': 9}}


@pytest.mark.parametrize('text', ['两个培养瓶分列木桌左右两侧。',
                                'Two culture flasks illustrating ATP合酶 concepts, on a wooden table.',
                                'One flask with synthetic rare-script \u3400 content.'])
def test_scene_cjk_is_retranslated_once_from_same_original_authorized_input(text):
    approved = slot()
    first = scene_value(text=text)
    final = scene_value(text='Two culture flasks, red on the left and blue on the right, on a wooden table, ATP synthase concept.')
    adapter = Scripted([output(first), output(final)])
    runtime = BuiltinAgentRuntime(SimpleNamespace(ollama=adapter))
    result = runtime.prompts([approved], '清楚表达，不在图片内生成文字', 'local-qwen')
    assert result['synthetic-slot']['text'] == final['text']
    assert len(adapter.calls) == 2
    assert adapter.calls[0]['payload'] == adapter.calls[1]['payload']
    assert adapter.calls[0]['schema'] == adapter.calls[1]['schema']
    assert all(call['model'] == 'local-qwen' for call in adapter.calls)
    assert approved['description'] in json.dumps(adapter.calls[1]['payload'], ensure_ascii=False)
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'scene_repair']
    first_evidence, repair = adapter.last_prompt_evidence
    assert first_evidence['result_valid'] is False and repair['result_valid'] is True
    assert repair['repair_reason'] == 'english' and repair['repaired_fields'] == ['text']
    assert repair['input_sha256'] == first_evidence['input_sha256']
    assert repair['done'] is True and repair['eval_count'] == 9


@pytest.mark.parametrize('field', ['text', 'negative'])
def test_only_bad_language_fields_can_change_and_rejected_draft_is_not_replayed(field):
    first = scene_value(**{field: '未转换文本 REJECTED_MODEL_DRAFT_CANARY'})
    replacement = scene_value(text='An unrelated invented stadium.', negative='unapproved new exclusion')
    adapter = Scripted([output(first), output(replacement)])
    result = adapter.prompts([slot()], '插图', 'local-qwen')['synthetic-slot']
    other = 'negative' if field == 'text' else 'text'
    assert result[other] == first[other]
    assert result[field] == replacement[field]
    assert 'REJECTED_MODEL_DRAFT_CANARY' not in json.dumps(adapter.calls)
    assert 'REJECTED_MODEL_DRAFT_CANARY' not in json.dumps(adapter.last_prompt_evidence)
    assert len(adapter.calls) == 2


@pytest.mark.parametrize('bad_value', [
    scene_value(text=''), scene_value(text=None), scene_value(text=True), scene_value(text='x' * 5501),
    scene_value(text='123'), scene_value(negative=None), scene_value(negative='x' * 3001),
    {**scene_value(text='中文'), 'extra': 'not-approved'}])
def test_malformed_or_out_of_bounds_scene_never_qualifies_for_language_retry(bad_value):
    adapter = Scripted([output(bad_value)])
    with pytest.raises(ModelError):
        adapter.prompts([slot()], '插图', 'local-qwen')
    assert len(adapter.calls) == 1
    assert adapter.last_prompt_evidence[0]['result_valid'] is False


@pytest.mark.parametrize('second', [output(scene_value(text='依然是中文')),
                                   output(scene_value(), 'changed-slot'),
                                   {'prompts': {}}, output({**scene_value(), 'extra': 'injected'})])
def test_still_invalid_or_changed_schema_correction_fails_after_exactly_one_attempt(second):
    adapter = Scripted([output(scene_value(text='原始中文')), second])
    with pytest.raises(ModelError):
        adapter.prompts([slot()], '插图', 'local-qwen')
    assert len(adapter.calls) == 2
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False


def test_scene_with_valid_language_never_performs_extra_request():
    adapter = Scripted([output(scene_value())])
    result = adapter.prompts([slot()], '插图', 'local-qwen')
    assert result == {'synthetic-slot': scene_value()}
    assert len(adapter.calls) == 1 and len(adapter.last_prompt_evidence) == 1


@pytest.mark.parametrize('regional', [False, True])
def test_initial_scene_outer_extra_field_rejects_before_any_language_correction(regional):
    response = {**output(scene_value(regional=regional, text='尚未转换的中文场景')),
                'unapproved_top_level': 'UNAPPROVED_EXTRA_FIELD'}
    adapter = Scripted([response, output(scene_value(regional=regional))])
    with pytest.raises(ModelError, match='外层结构'):
        adapter.prompts([slot(regional=regional)], '插图', 'local-qwen')
    assert len(adapter.calls) == 1
    assert len(adapter.last_prompt_evidence) == 1
    assert adapter.last_prompt_evidence[0]['result_valid'] is False
    assert adapter.last_prompt_evidence[0]['request_state'] == 'completed'
    field_checks = adapter.last_prompt_evidence[0]['field_checks']
    assert field_checks['text']['has_cjk'] is True
    assert 'UNAPPROVED_EXTRA_FIELD' not in json.dumps(adapter.last_prompt_evidence)


@pytest.mark.parametrize('language_repair', [True, False])
def test_repaired_scene_outer_extra_field_is_rejected_without_another_attempt(language_repair):
    first = scene_value(regional=True, text='尚未转换的中文场景' if language_repair else 'One flask without a table.')
    corrected = {**output(scene_value(regional=True)), 'unapproved_top_level': 'UNAPPROVED_EXTRA_FIELD'}
    adapter = Scripted([output(first), corrected])
    with pytest.raises(ModelError):
        adapter.prompts([slot(regional=True)], '插图', 'local-qwen')
    assert len(adapter.calls) == 2
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'scene_repair']
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False


def test_regional_scene_and_negative_correct_in_one_budget_then_isolated_objects_replace_drafts():
    approved = slot(regional=True)
    valid = scene_value(regional=True)
    adapter = Scripted([
        output(scene_value(regional=True, text='中文场景', negative='伪字、水印')),
        output({**valid, 'background_text': 'UNAPPROVED_CHANGED_BACKGROUND',
                'regions': [{'object_id': 'blue-flask', 'text': 'REPAIRED_SHARED_REGION_DRAFT'}]}),
        {'object_id': 'red-flask', 'text': 'One red culture flask.'},
        {'object_id': 'blue-flask', 'text': 'One blue culture flask.'}])
    result = adapter.prompts([approved], '插图', 'local-qwen')['synthetic-slot']
    assert len(adapter.calls) == 4
    assert result['text'] == valid['text'] and result['negative'] == valid['negative']
    assert result['background_text'] == 'A wooden table and a plain wall.'
    assert result['regions'] == [{'object_id': 'red-flask', 'text': 'One red culture flask.'},
                                 {'object_id': 'blue-flask', 'text': 'One blue culture flask.'}]
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'scene_repair', 'object', 'object']
    assert adapter.last_prompt_evidence[1]['repaired_fields'] == ['text', 'negative']
    assert adapter.calls[2]['payload'] == {'object': {'id': 'red-flask', 'label': '红色培养瓶', 'description': '一个红色培养瓶'}}
    assert adapter.calls[3]['payload'] == {'object': {'id': 'blue-flask', 'label': '蓝色培养瓶', 'description': '一个蓝色培养瓶'}}


def test_ignored_shared_region_language_does_not_trigger_unnecessary_scene_repair():
    adapter = Scripted([output(scene_value(regional=True, regions=[{'object_id': 'red-flask', 'text': '应丢弃的中文共享草稿'}])),
                        {'object_id': 'red-flask', 'text': 'One red culture flask.'},
                        {'object_id': 'blue-flask', 'text': 'One blue culture flask.'}])
    result = adapter.prompts([slot(regional=True)], '插图', 'local-qwen')
    assert len(adapter.calls) == 3 and '应丢弃' not in json.dumps(result, ensure_ascii=False)
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'object', 'object']


def test_independent_background_and_object_corrections_preserve_separate_original_contexts():
    approved = slot(regional=True, background=True)
    adapter = Scripted([
        output(scene_value(regional=True, background_text='这个共享中文背景必须丢弃')),
        {'slot_id': 'synthetic-slot', 'text': '浅色木桌和素色墙面'},
        {'slot_id': 'synthetic-slot', 'text': 'A pale wooden table and a plain wall.'},
        {'object_id': 'red-flask', 'text': '一个红色培养瓶'},
        {'object_id': 'red-flask', 'text': 'One red culture flask.'},
        {'object_id': 'blue-flask', 'text': 'One blue culture flask.'}])
    result = adapter.prompts([approved], 'PRIVATE_APPROVED_STYLE', 'local-qwen')['synthetic-slot']
    assert len(adapter.calls) == 6
    assert result['background_text'] == 'A pale wooden table and a plain wall.'
    assert adapter.calls[1]['payload'] == adapter.calls[2]['payload'] == {
        'slot_id': 'synthetic-slot', 'background': approved['director']['background']}
    assert adapter.calls[3]['payload'] == adapter.calls[4]['payload'] == {
        'object': {key: approved['director']['objects'][0][key] for key in ('id', 'label', 'description')}}
    for index in (1, 2, 3, 4, 5):
        assert 'PRIVATE_APPROVED_STYLE' not in json.dumps(adapter.calls[index]['payload'])
        assert approved['description'] not in json.dumps(adapter.calls[index]['payload'], ensure_ascii=False)
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'background', 'background_repair',
                                                               'object', 'object_repair', 'object']
    assert all(e['result_valid'] for e in adapter.last_prompt_evidence if e['phase'].endswith('_repair'))
    for index in (1, 3):
        assert adapter.last_prompt_evidence[index]['field_checks']['text']['has_cjk'] is True
        assert adapter.last_prompt_evidence[index]['field_checks']['text']['has_ascii'] is False
    for index in (2, 4):
        assert adapter.last_prompt_evidence[index]['field_checks']['text']['has_cjk'] is False
        assert adapter.last_prompt_evidence[index]['field_checks']['text']['has_ascii'] is True
        assert set(adapter.last_prompt_evidence[index]['field_checks']) == {'text'}


def test_language_and_exclusion_correction_share_one_scene_attempt_budget():
    adapter = Scripted([output(scene_value(regional=True, text='中文场景')),
                        output(scene_value(regional=True, text='One flask without a table.'))])
    with pytest.raises(ModelError):
        adapter.prompts([slot(regional=True)], '插图', 'local-qwen')
    assert len(adapter.calls) == 2
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'scene_repair']
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False


@pytest.mark.parametrize('role', ['background', 'object'])
def test_failed_isolated_language_correction_cannot_fall_back_to_a_shared_scene(role):
    approved = slot(regional=True, background=role == 'background')
    first = {'slot_id': 'synthetic-slot', 'text': '浅色木桌'} if role == 'background' else {
        'object_id': 'red-flask', 'text': '一个红色培养瓶'}
    changed = {'slot_id': 'WRONG', 'text': 'A wooden table.'} if role == 'background' else {
        'object_id': 'WRONG', 'text': 'One red culture flask.'}
    adapter = Scripted([output(scene_value(regional=True)), first, changed])
    with pytest.raises(ModelError):
        adapter.prompts([approved], '插图', 'local-qwen')
    assert len(adapter.calls) == 3
    assert adapter.calls[1]['payload'] == adapter.calls[2]['payload']
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', role, role + '_repair']
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False
    assert adapter.last_prompt_evidence[-1]['input_sha256'] == adapter.last_prompt_evidence[-2]['input_sha256']


def test_scene_language_correction_keeps_successful_independent_background_without_retranslating_it():
    approved = slot(regional=True, background=True)
    adapter = Scripted([
        output(scene_value(regional=True, text='一个红瓶与一个蓝瓶')),
        {'slot_id': 'synthetic-slot', 'text': 'A pale wooden table and a plain wall.'},
        output(scene_value(regional=True, background_text='A different invented environment.')),
        {'object_id': 'red-flask', 'text': 'One red culture flask.'},
        {'object_id': 'blue-flask', 'text': 'One blue culture flask.'}])
    result = adapter.prompts([approved], '插图', 'local-qwen')['synthetic-slot']
    assert result['background_text'] == 'A pale wooden table and a plain wall.'
    assert len(adapter.calls) == 5 and sum('background' in call['payload'] for call in adapter.calls) == 1
    assert [e['phase'] for e in adapter.last_prompt_evidence] == ['scene', 'background', 'scene_repair', 'object', 'object']


def test_exclusion_repair_does_not_start_a_third_call_for_new_chinese_output():
    adapter = Scripted([output(scene_value(regional=True, text='One flask without a table.')),
                        output(scene_value(regional=True, negative='伪字、水印'))])
    with pytest.raises(ModelError):
        adapter.prompts([slot(regional=True)], '插图', 'local-qwen')
    assert len(adapter.calls) == 2 and adapter.last_prompt_evidence[-1]['result_valid'] is False


def install_transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('failure', ['timeout', 'truncated', 'malformed'])
@pytest.mark.parametrize('during_repair', [False, True])
def test_unknown_or_incomplete_call_is_not_blindly_reissued(monkeypatch, failure, during_repair):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == 'http://127.0.0.1:11434/api/chat'
        if during_repair and len(calls) == 1:
            return httpx.Response(200, json={'done': True, 'message': {'content': json.dumps(output(scene_value(text='中文')))}})
        if failure == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout')
        if failure == 'truncated':
            return httpx.Response(200, json={'done': True, 'done_reason': 'length',
                                           'message': {'content': json.dumps(output(scene_value()))}})
        return httpx.Response(200, json={'done': True, 'message': {'content': '{"prompts": '}})
    install_transport(monkeypatch, handler)
    adapter = OllamaAdapter()
    with pytest.raises((ModelError, httpx.ReadTimeout)):
        adapter.prompts([slot()], '插图', 'local-qwen')
    assert len(calls) == (2 if during_repair else 1)
    assert adapter.last_prompt_evidence[-1]['request_state'] == 'failed'
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False
    assert all(check['type'] == 'missing' for check in adapter.last_prompt_evidence[-1]['field_checks'].values())


@pytest.mark.parametrize('value,kind,length,blank,cjk,ascii_letters', [
    ('', 'string', 0, True, False, False), (' \n\t', 'string', 3, True, False, False),
    ('纯中文', 'string', 3, False, True, False), ('ATP合酶', 'string', 5, False, True, True),
    ('One leaf.', 'string', 9, False, False, True), ('123', 'string', 3, False, False, False),
    (None, 'null', None, None, None, None), (False, 'boolean', None, None, None, None),
    (17, 'number', None, None, None, None), ([], 'array', 0, None, None, None),
    ({'SYNTHETIC_PRIVATE_KEY': 'SYNTHETIC_PRIVATE_VALUE'}, 'object', 1, None, None, None)])
def test_private_field_diagnostics_record_only_fixed_types_and_flags(value, kind, length, blank, cjk, ascii_letters):
    record = OllamaAdapter.prompt_field_checks({'text': value}, ('text',))
    assert record == {'text': {'type': kind, 'length': length, 'blank': blank, 'has_cjk': cjk, 'has_ascii': ascii_letters}}
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(record)
    missing = OllamaAdapter.prompt_field_checks({}, ('text',))['text']
    assert missing == {'type': 'missing', 'length': None, 'blank': None, 'has_cjk': None, 'has_ascii': None}


def test_failed_scene_and_failed_correction_keep_separate_body_free_diagnostics_without_extra_calls():
    first = scene_value(text='SYNTHETIC_PRIVATE_DRAFT中文')
    second = scene_value(text=' \n\t')
    adapter = Scripted([output(first), output(second)])
    with pytest.raises(ModelError):
        adapter.prompts([slot()], '插图', 'local-qwen')
    assert len(adapter.calls) == 2
    initial, repair = adapter.last_prompt_evidence
    assert initial['field_checks']['text'] == {'type': 'string', 'length': len(first['text']), 'blank': False,
                                              'has_cjk': True, 'has_ascii': True}
    assert repair['field_checks']['text'] == {'type': 'string', 'length': 3, 'blank': True,
                                             'has_cjk': False, 'has_ascii': False}
    assert 'SYNTHETIC_PRIVATE_DRAFT' not in json.dumps(adapter.last_prompt_evidence)
    assert initial['result_valid'] is False and repair['result_valid'] is False


def test_real_adapter_wire_protocol_keeps_original_ids_schema_local_model_and_fixed_bounds(monkeypatch):
    calls = []
    approved = slot()
    def handler(request):
        sent = json.loads(request.content)
        calls.append(sent)
        value = scene_value(text='One synthetic ATP合酶 illustration.') if len(calls) == 1 else scene_value()
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'model': 'local-qwen',
                                        'eval_count': 20, 'message': {'content': json.dumps(output(value))}})
    install_transport(monkeypatch, handler)
    adapter = OllamaAdapter()
    result = adapter.prompts([approved], '插图', 'local-qwen')
    assert result == {'synthetic-slot': scene_value()}
    assert len(calls) == 2
    assert calls[0]['messages'][1] == calls[1]['messages'][1]
    assert calls[0]['format'] == calls[1]['format'] == adapter.scene_prompt_schema(approved)
    assert all(call['model'] == 'local-qwen' and call['options']['temperature'] == 0 and call['keep_alive'] == 0
               for call in calls)
    assert all(e['model'] == 'local-qwen' and e['eval_count'] == 20 for e in adapter.last_prompt_evidence)


def test_all_generation_content_schemas_constrain_printable_ascii_but_keep_chinese_ids_and_analysis():
    approved = slot(regional=True)
    approved['id'] = '图位-一'
    approved['director']['objects'][0]['id'] = '物体-一'
    scene = OllamaAdapter.scene_prompt_schema(approved)
    regional = OllamaAdapter.regional_prompt_schema(approved)
    object_schema = OllamaAdapter.object_prompt_schema('物体-一')
    background_schema = OllamaAdapter.background_prompt_schema('图位-一')
    scene_fields = scene['properties']['prompts']['properties']['图位-一']['properties']
    regional_fields = regional['properties']['prompts']['properties']['图位-一']['properties']
    content_fields = [(scene_fields['text'], 5500, False), (scene_fields['negative'], 3000, True),
                      (regional_fields['text'], 5500, False), (regional_fields['negative'], 3000, True),
                      (regional_fields['background_text'], 2500, False),
                      (regional_fields['regions']['items']['properties']['text'], 650, False),
                      (object_schema['properties']['text'], 650, False),
                      (background_schema['properties']['text'], 650, False)]
    for field, maximum, empty_allowed in content_fields:
        assert field['type'] == 'string' and field['maxLength'] == maximum
        assert field['pattern'] == ('^[ -~]*$' if empty_allowed else '^[ -~]+$')
        assert bool(re.fullmatch(field['pattern'], '')) is empty_allowed
        assert re.fullmatch(field['pattern'], 'ATP synthase at 37 C, pH 7.4; left-to-right.')
        for unsupported in ('ATP合酶', 'cell\u2019s', 'alpha\u2013beta', 'A\nB', 'A\tB', 'cell\U0001f33f'):
            assert re.fullmatch(field['pattern'], unsupported) is None
    assert object_schema['properties']['object_id'] == {'type': 'string', 'enum': ['物体-一']}
    assert background_schema['properties']['slot_id'] == {'type': 'string', 'enum': ['图位-一']}
    assert regional_fields['regions']['items']['properties']['object_id']['enum'] == ['物体-一', 'blue-flask']
    assert scene['properties']['prompts']['required'] == ['图位-一']
    assert approved['description'] == slot(regional=True)['description']
    analysis = OllamaAdapter.analysis_schema([{'id': '段落-一', 'kind': 'paragraph'}])
    assert 'pattern' not in analysis['properties']['summary']
    assert 'pattern' not in analysis['properties']['slots']['items']['properties']['description']
    assert analysis['properties']['coverage']['items']['enum'] == ['段落-一']


def test_generation_roles_explicitly_require_english_ascii_without_touching_chinese_inputs():
    from backend.app import community_prompts
    instruction = ('Use English words and ASCII punctuation; transliterate CJK proper names, '
                   'preserve existing Latin acronyms; do not append Chinese original labels.')
    assert all(instruction in prompt for prompt in (community_prompts.SCENE, community_prompts.OBJECT,
                                                   community_prompts.BACKGROUND))
    approved = slot()
    adapter = Scripted([output(scene_value())])
    adapter.prompts([approved], '中文风格要求', 'local-qwen')
    assert len(adapter.calls) == 1
    sent = adapter.calls[0]
    assert sent['payload']['style'] == '中文风格要求'
    assert sent['payload']['slots'][0]['description'] == approved['description']
    assert sent['payload']['slots'][0]['id'] == approved['id']
