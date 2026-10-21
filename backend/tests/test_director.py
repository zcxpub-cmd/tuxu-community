"""Director validation and actual standard-node graph contracts, without inference."""
import copy

import pytest

from backend.app.director import (
    BRIGHTNESS, CAMERAS, LIGHTING, STYLES, compile_controls, default_director,
    generation_text, placement_text, region_areas, validate_director,
)
from backend.app.main import digest, prompt_hash, validate_prompts
from backend.app.models import ComfyAdapter, OllamaAdapter


def configured():
    return {**default_director(), 'style_id': 'watercolor', 'brightness': 'low', 'lighting': 'backlit', 'camera': 'topdown',
            'notes': '保持夜晚氛围', 'objects': [
                {'id': 'tree', 'label': '树木', 'description': '绿色叶片', 'x': 4, 'y': 7, 'width': 35, 'height': 60, 'depth': 'foreground'},
                {'id': 'building', 'label': '建筑', 'description': '', 'x': 72, 'y': 60, 'width': 28, 'height': 40, 'depth': 'background'},
            ]}


@pytest.mark.parametrize('field,bad_value', [
    ('schema_version', True), ('style_id', 'invented'), ('brightness', 'very bright'),
    ('lighting', []), ('camera', 'telephoto'), ('notes', 'x' * 601), ('objects', {}), ('unknown', 'value'),
])
def test_director_rejects_unsupported_controls(field, bad_value):
    config = configured()
    config[field] = bad_value
    with pytest.raises(ValueError):
        validate_director(config)


@pytest.mark.parametrize('field,bad_value', [
    ('x', True), ('x', '20'), ('x', float('nan')), ('height', float('inf')),
    ('x', -1), ('x', 90), ('width', 7.9), ('height', 101), ('label', ''),
    ('description', 'x' * 301), ('depth', 'invented'), ('unknown', 'value'),
])
def test_director_rejects_invalid_or_outside_object(field, bad_value):
    config = configured()
    config['objects'][0][field] = bad_value
    with pytest.raises(ValueError):
        validate_director(config)


def test_director_duplicate_ids_and_five_objects_are_not_silently_trimmed():
    config = configured()
    config['objects'][1]['id'] = config['objects'][0]['id']
    with pytest.raises(ValueError):
        validate_director(config)
    config = configured()
    config['objects'] = [config['objects'][0]] * 5
    with pytest.raises(ValueError):
        validate_director(config)


@pytest.mark.parametrize('aspect,size', [('landscape', (1152, 768)), ('square', (1024, 1024)), ('portrait', (768, 1152))])
def test_percentage_boxes_map_to_supported_nodes_and_stay_inside(aspect, size):
    config = configured()
    config['objects'][1].update(x=92, y=92, width=8, height=8)
    slot = {'id': 's', 'aspect_ratio': aspect, 'director': validate_director(config)}
    prompt = {'regions': [{'object_id': 'tree', 'text': 'Green tree.'}, {'object_id': 'building', 'text': 'A small house.'}]}
    areas = region_areas(slot, prompt)
    assert areas[0]['x'] <= 4 / 100 * size[0]
    assert areas[0]['x'] + areas[0]['width'] >= 39 / 100 * size[0]
    for area in areas:
        assert area['strength'] == 1.0
        assert all(area[key] % 8 == 0 for key in ('x', 'y', 'width', 'height'))
        assert min(area['width'], area['height']) >= 64
        assert area['x'] >= 0 and area['x'] + area['width'] <= size[0]
        assert area['y'] >= 0 and area['y'] + area['height'] <= size[1]
    graph = ComfyAdapter.workflow('test-checkpoint', 'Whole scene.', 'text', 7, aspect, areas)
    assert graph['5']['inputs']['positive'] == ['13', 0]
    assert graph['9']['class_type'] == graph['12']['class_type'] == 'ConditioningSetArea'
    assert graph['10']['inputs']['conditioning_1'] == ['2', 0]
    assert graph['13']['inputs']['conditioning_1'] == ['10', 0]
    assert graph['9']['inputs']['x'] == areas[0]['x']
    assert graph['12']['inputs']['width'] == areas[1]['width']
    assert graph['3']['inputs']['text'] == 'text'
    assert graph['4']['inputs']['width'] == size[0]
    assert len(ComfyAdapter.workflow('test-checkpoint', 'Old scene.', 'text', 7, aspect)) == 7


def test_visible_controls_are_complete_and_idempotent_after_user_edits():
    config = configured()
    slots = [{'id': 's', 'director': config}]
    prompts = {'s': {'text': 'An approved scene.', 'negative': 'text', 'regions': [
        {'object_id': 'tree', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A house.'}]}}
    compiled = compile_controls(prompts, slots)
    assert compile_controls(compiled, slots) == compiled
    for token in (STYLES['watercolor'], BRIGHTNESS['low'], LIGHTING['backlit'], CAMERAS['topdown']):
        assert token in compiled['s']['text']
    assert 'foreground' in compiled['s']['regions'][0]['text']
    assert compiled['s']['regions'][0]['text'].startswith('A tree.\n')
    assert compiled['s']['global_text'] == compiled['s']['text']
    assert 'Director controls:' not in compiled['s']['global_text']
    compiled['s']['text'] += '\nUser changed a scene detail.'
    edited = compile_controls(compiled, slots)
    assert edited['s']['text'].count(BRIGHTNESS['low']) == 1
    assert 'User changed a scene detail.' in edited['s']['text']
    assert compile_controls(edited, slots) == edited


@pytest.mark.parametrize('x,y,expected', [(0, 0, 'upper-left'), (42, 0, 'upper-center'), (84, 0, 'upper-right'),
    (0, 42, 'middle-left'), (42, 42, 'center'), (84, 42, 'middle-right'),
    (0, 84, 'lower-left'), (42, 84, 'lower-center'), (84, 84, 'lower-right')])
def test_position_language_is_natural_nine_grid_without_numbers_or_ui_labels(x, y, expected):
    item = {'id': 'secret-id', 'label': 'secret-label', 'x': x, 'y': y, 'width': 16, 'height': 16}
    assert placement_text(item) == f'{expected} area'


def test_background_is_compiled_before_approval_and_hashes_only_when_present():
    slot = {'id': 's', 'director': configured()}
    old = {'s': {'text': 'A scene.', 'negative': 'text', 'regions': [
        {'object_id': 'tree', 'text': 'A green tree.'}, {'object_id': 'building', 'text': 'A house.'}]}}
    assert prompt_hash({'prompts': old}) == digest(old)
    newer = copy.deepcopy(old)
    newer['s']['background_text'] = 'A neutral ground and soft surrounding light.'
    clean = validate_prompts(newer, [slot], english=True, require_background=True)
    compiled = compile_controls(clean, [slot])
    assert compile_controls(compiled, [slot]) == compiled
    assert compiled['s']['background_text'].count(BRIGHTNESS['low']) == 1
    assert compiled['s']['regions'][0]['text'].startswith('A green tree.\n')
    assert 'middle-left' in compiled['s']['regions'][0]['text']
    assert 'lower-right' in compiled['s']['regions'][1]['text']
    assert prompt_hash({'prompts': compiled}) != prompt_hash({'prompts': old})
    changed = copy.deepcopy(compiled)
    changed['s']['background_text'] += ' Reflected light.'
    assert prompt_hash({'prompts': changed}) != prompt_hash({'prompts': compiled})


def test_global_scene_is_frozen_hashed_and_has_legacy_byte_preserving_selector():
    slot = {'id': 's', 'director': configured()}
    old = {'text': 'Legacy scene.\nDirector controls: legacy unchanged.', 'negative': 'text',
           'background_text': ' Legacy background with whitespace.\n', 'regions': [
               {'object_id': 'tree', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A house.'}]}
    assert generation_text(old, regional=True) == old['background_text']
    assert prompt_hash({'prompts': {'s': old}}) == digest({'s': old})
    text_only = {'text': ' Legacy complete scene.\n', 'negative': 'text'}
    assert generation_text(text_only, regional=True) == text_only['text']
    compiled = compile_controls({'s': old}, [slot])
    assert generation_text(compiled['s'], regional=True) == compiled['s']['global_text']
    assert 'Legacy scene.' in compiled['s']['global_text']
    assert 'Director controls:' not in compiled['s']['global_text']
    changed = copy.deepcopy(compiled)
    changed['s']['global_text'] += ' A second detail.'
    assert prompt_hash({'prompts': changed}) != prompt_hash({'prompts': compiled})
    assert generation_text(compiled['s']) == compiled['s']['text']


@pytest.mark.parametrize('global_value', [None, '', '中文场景', '1234', 'x' * 6001, True, [], {}])
def test_global_scene_private_field_rejects_invalid_values(global_value):
    from fastapi import HTTPException
    prompts = {'s': {'text': 'A scene.', 'negative': 'text', 'global_text': global_value, 'regions': [
        {'object_id': 'tree', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A house.'}]}}
    with pytest.raises(HTTPException):
        validate_prompts(prompts, [{'id': 's', 'director': configured()}], english=True)


def test_global_scene_requires_regions_and_preserves_valid_private_text():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        validate_prompts({'s': {'text': 'A scene.', 'negative': 'text', 'global_text': 'A scene.'}}, [{'id': 's'}])
    prompts = {'s': {'text': 'A scene.', 'negative': 'text', 'global_text': '  Full scene.  ', 'regions': [
        {'object_id': 'tree', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A house.'}]}}
    assert validate_prompts(prompts, [{'id': 's', 'director': configured()}], english=True)['s']['global_text'] == 'Full scene.'


@pytest.mark.parametrize('identity', [
    'A cup (red): glazed.\nSmooth ceramic.',
    'A cup (red (matte)): size 1.5. One handle.',
    'A cup: 9\nRed ceramic.',
    'A cup (unclosed.\nOne handle.',
    'A cup \\(red\\).\nSmooth ceramic.',
])
def test_region_text_escapes_parentheses_and_is_idempotent(identity):
    slot = {'id': 's', 'director': configured()}
    prompts = {'s': {'text': 'A cup beside a house.', 'negative': 'text', 'regions': [
        {'object_id': 'tree', 'text': identity}, {'object_id': 'building', 'text': 'A house.'}]}}
    compiled = compile_controls(prompts, [slot])
    assert compile_controls(compiled, [slot]) == compiled
    region = compiled['s']['regions'][0]['text']
    assert region.startswith(identity.replace('\\(', '(').replace('\\)', ')').replace('(', '\\(').replace(')', '\\)'))
    assert compiled['s']['negative'] == 'text'


@pytest.mark.parametrize('background', [None, '', '中文背景', 'x' * 3001, False])
def test_new_regional_approval_requires_valid_english_background(background):
    from fastapi import HTTPException
    prompt = {'text': 'A scene.', 'negative': 'text', 'regions': [
        {'object_id': 'tree', 'text': 'A green tree.'}, {'object_id': 'building', 'text': 'A house.'}]}
    if background is not None:
        prompt['background_text'] = background
    with pytest.raises(HTTPException):
        validate_prompts({'s': prompt}, [{'id': 's', 'director': configured()}], english=True, require_background=True)


def test_nonregional_background_is_rejected_and_legacy_region_validation_is_unchanged():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        validate_prompts({'s': {'text': 'A scene.', 'negative': 'text', 'background_text': 'A surface.'}}, [{'id': 's'}])
    old = {'s': {'text': 'A scene.', 'negative': 'text', 'regions': [
        {'object_id': 'tree', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A house.'}]}}
    assert validate_prompts(old, [{'id': 's', 'director': configured()}], english=True) == old


def test_legacy_prompt_hash_and_compilation_keep_exact_old_form():
    prompts = {'old': {'text': 'A tree.', 'negative': 'text'}}
    assert prompt_hash({'prompts': prompts}) == digest(prompts)
    assert compile_controls(prompts, [{'id': 'old'}]) == prompts
    assert validate_prompts(prompts, [{'id': 'old'}]) == prompts


def test_regions_are_canonical_and_part_of_fingerprint():
    slot = {'id': 's', 'director': configured()}
    prompts = {'s': {'text': 'A scene.', 'negative': 'text', 'regions': [
        {'object_id': 'building', 'text': 'A house.'}, {'object_id': 'tree', 'text': 'A tree.'}]}}
    canonical = validate_prompts(prompts, [slot], english=True)
    assert [region['object_id'] for region in canonical['s']['regions']] == ['tree', 'building']
    changed = copy.deepcopy(canonical)
    changed['s']['regions'][0]['text'] = 'A green tree.'
    assert prompt_hash({'prompts': canonical}) != prompt_hash({'prompts': changed})


@pytest.mark.parametrize('regions', [
    None, [], [{'object_id': 'tree', 'text': 'A tree.'}] * 2,
    [{'object_id': 'unknown', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A house.'}],
    [{'object_id': 'tree', 'text': '中文'}, {'object_id': 'building', 'text': 'A house.'}],
    [{'object_id': 'tree', 'text': 'x' * 1201}, {'object_id': 'building', 'text': 'A house.'}],
])
def test_regions_cannot_omit_duplicate_invent_or_leave_untranslated(regions):
    from fastapi import HTTPException
    prompts = {'s': {'text': 'A scene.', 'negative': 'text', 'regions': regions}}
    with pytest.raises(HTTPException):
        validate_prompts(prompts, [{'id': 's', 'director': configured()}], english=True)


def test_nonempty_regions_without_director_objects_are_rejected():
    from fastapi import HTTPException
    prompts = {'old': {'text': 'A tree.', 'negative': 'text', 'regions': [{'object_id': 'unknown', 'text': 'A tree.'}]}}
    with pytest.raises(HTTPException):
        validate_prompts(prompts, [{'id': 'old'}], english=True)


def test_translator_receives_all_approved_director_details_without_a_new_request_type():
    class CapturingAdapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            if 'object' in payload:
                item = payload['object']
                self.object_payloads = getattr(self, 'object_payloads', []) + [payload]
                return {'object_id': item['id'], 'text': 'A green tree.' if item['id'] == 'tree' else 'A building.'}
            self.payload = payload
            assert system == __import__('backend.app.community_prompts', fromlist=['SCENE']).SCENE
            assert isinstance(output_format, dict)
            return {'prompts': {'s': {'text': 'An approved scene.', 'negative': 'text',
                'background_text': 'A neutral ground.', 'regions': [
                {'object_id': 'tree', 'text': 'A tree beside a building.'}, {'object_id': 'building', 'text': 'A building with a tree.'}]}}}
    adapter = CapturingAdapter()
    config = configured()
    result = adapter.prompts([{'id': 's', 'description': '原批准画面', 'director': config}], '统一风格', 'fake-text')
    assert adapter.payload['slots'][0]['director'] == config
    assert adapter.payload['slots'][0]['description'] == '原批准画面'
    assert STYLES['watercolor'] in adapter.payload['slots'][0]['fixed_visual_controls']
    assert adapter.payload['slots'][0]['fixed_object_placements'] == [
        {'id': item['id'], 'label': item['label'], 'placement': placement_text(item)} for item in config['objects']]
    assert adapter.object_payloads == [{'object': {key: item[key] for key in ('id', 'label', 'description')}} for item in config['objects']]
    assert result['s']['regions'] == [{'object_id': 'tree', 'text': 'A green tree.'}, {'object_id': 'building', 'text': 'A building.'}]
    assert [(item.get('phase'), item.get('object_id')) for item in adapter.last_prompt_evidence] == [('scene', None), ('object', 'tree'), ('object', 'building')]


@pytest.mark.parametrize('regional', [False, True])
def test_actual_ollama_request_uses_schema_only_for_regional_single_slot(monkeypatch, regional):
    import json
    import httpx
    actual_client = httpx.Client
    slot = {'id': 'slot-dynamic:123', 'description': '原批准画面'}
    value = {'text': 'A tree beside a building.', 'negative': 'text'}
    if regional:
        slot['director'] = configured()
        value.update(background_text='A neutral ground in soft light.', regions=[
            {'object_id': 'tree', 'text': 'A green tree.'}, {'object_id': 'building', 'text': 'A building.'}])
    requests = []
    def response(request):
        sent = json.loads(request.content)
        requests.append(sent)
        payload = json.loads(sent['messages'][1]['content'])
        result = {'prompts': {slot['id']: value}}
        if 'object' in payload:
            item = payload['object']
            result = {'object_id': item['id'], 'text': 'A green tree.' if item['id'] == 'tree' else 'A building.'}
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop',
            'message': {'content': json.dumps(result)}})
    transport = httpx.MockTransport(response)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: actual_client(transport=transport, **kwargs))
    adapter = OllamaAdapter()
    prompts = adapter.prompts([slot], '统一风格', 'fake-text')
    assert prompts == {slot['id']: value} and len(requests) == (3 if regional else 1)
    sent = requests[0]
    assert sent['keep_alive'] == 0 and sent['stream'] is False
    if not regional:
        assert sent['format'] == 'json' and sent['options']['temperature'] == 0.15
        assert 'required schema:' not in sent['messages'][0]['content']
        assert 'background_text' not in prompts[slot['id']]
        return
    schema = sent['format']
    assert sent['options']['temperature'] == 0
    assert json.dumps(schema, ensure_ascii=False) in sent['messages'][0]['content']
    assert schema['required'] == ['prompts'] and schema['additionalProperties'] is False
    slots_schema = schema['properties']['prompts']
    assert slots_schema['required'] == [slot['id']] and slots_schema['additionalProperties'] is False
    fields = slots_schema['properties'][slot['id']]
    assert set(fields['required']) == {'text', 'negative', 'background_text', 'regions'}
    for name in ('text', 'negative', 'background_text'):
        assert fields['properties'][name]['type'] == 'string' and fields['properties'][name]['maxLength'] > 0
    regions = fields['properties']['regions']
    assert regions['minItems'] == regions['maxItems'] == 2
    assert regions['items']['properties']['object_id']['enum'] == ['tree', 'building']
    assert regions['items']['properties']['text']['maxLength'] == 650
    assert regions['items']['required'] == ['object_id', 'text'] and regions['items']['additionalProperties'] is False
    placements = json.loads(sent['messages'][1]['content'])['slots'][0]['fixed_object_placements']
    assert placements == [{'id': item['id'], 'label': item['label'], 'placement': placement_text(item)} for item in slot['director']['objects']]
    assert adapter.last_prompt_evidence[0]['fixed_object_placements'] == placements
    for request, item in zip(requests[1:], slot['director']['objects']):
        assert request['keep_alive'] == 0 and request['stream'] is False and request['options']['temperature'] == 0
        assert json.loads(request['messages'][1]['content']) == {'object': {key: item[key] for key in ('id', 'label', 'description')}}
        object_schema = request['format']
        assert object_schema['required'] == ['object_id', 'text'] and object_schema['additionalProperties'] is False
        assert object_schema['properties']['object_id']['enum'] == [item['id']]
        assert object_schema['properties']['text']['maxLength'] == 650
        assert json.dumps(object_schema, ensure_ascii=False) in request['messages'][0]['content']
        from backend.app.community_prompts import OBJECT
        assert request['messages'][0]['content'].startswith(OBJECT)
    assert len(adapter.last_prompt_evidence) == 3 and all(item.get('done') is True for item in adapter.last_prompt_evidence)


def test_mock_ignoring_required_background_schema_is_rejected_without_default_fallback(monkeypatch):
    import json
    import httpx
    from backend.app.models import ModelError
    actual_client = httpx.Client
    slot = {'id': 's', 'description': '原批准画面', 'director': configured()}
    missing = {'text': 'A tree beside a building.', 'negative': 'text', 'regions': [
        {'object_id': 'tree', 'text': 'A tree.'}, {'object_id': 'building', 'text': 'A building.'}]}
    requests = []
    def response(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop',
            'message': {'content': json.dumps({'prompts': {'s': missing}})}})
    transport = httpx.MockTransport(response)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: actual_client(transport=transport, **kwargs))
    with pytest.raises(ModelError):
        OllamaAdapter().prompts([slot], '统一风格', 'fake-text')
    assert len(requests) == 1 and 'background_text' not in missing


@pytest.mark.parametrize('object_response', [
    {'object_id': 'wrong', 'text': 'A green tree.'},
    {'object_id': 'tree', 'text': ''},
    {'object_id': 'tree', 'text': '绿色树木'},
    {'object_id': 'tree', 'text': 'x' * 651},
    {'object_id': 'tree', 'text': True},
    {'object_id': 'tree', 'text': 'A tree without a building.'},
    {'object_id': 'tree', 'text': 'A tree, no text.'},
    {'object_id': 'tree', 'text': 'A green tree.', 'extra': 'A building.'},
])
def test_invalid_isolated_object_conversion_has_no_shared_region_fallback(object_response):
    from backend.app.models import ModelError
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.calls = getattr(self, 'calls', 0) + 1
            if 'object' in payload:
                return object_response
            return {'prompts': {'s': {'text': 'A tree beside a building.', 'negative': 'text',
                'background_text': 'A neutral ground.', 'regions': [
                    {'object_id': 'tree', 'text': 'A shared tree.'}, {'object_id': 'building', 'text': 'A shared house.'}]}}}
    adapter = Adapter()
    with pytest.raises(ModelError):
        adapter.prompts([{'id': 's', 'description': '批准画面', 'director': configured()}], '统一风格', 'fake-text')
    assert adapter.calls == 2
    assert [item['phase'] for item in adapter.last_prompt_evidence] == ['scene', 'object']


@pytest.mark.parametrize('bad_text', ['A tree with no writing.', 'A scene without words.', 'Avoid signs.', 'Do not add a house.'])
def test_regional_scene_exclusion_syntax_stops_after_one_failed_repair_before_object_calls(bad_text):
    from backend.app.models import ModelError
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.calls = getattr(self, 'calls', 0) + 1
            return {'prompts': {'s': {'text': bad_text, 'negative': 'text', 'background_text': 'A neutral ground.', 'regions': []}}}
    adapter = Adapter()
    with pytest.raises(ModelError):
        adapter.prompts([{'id': 's', 'description': '批准画面', 'director': configured()}], '统一风格', 'fake-text')
    assert adapter.calls == 2
    assert [item['phase'] for item in adapter.last_prompt_evidence] == ['scene', 'scene_repair']
    assert adapter.last_prompt_evidence[-1]['result_valid'] is False


def test_actual_scene_repair_uses_same_original_payload_and_schema_then_isolates_objects(monkeypatch):
    import json
    import httpx
    actual_client = httpx.Client
    slot = {'id': 's', 'description': '批准的树木和房子', 'director': configured()}
    rejected_draft = 'One tree beside a building without a roof. REJECTED_DRAFT_BODY.'
    requests, scene_calls = [], []
    def response(request):
        sent = json.loads(request.content)
        requests.append(sent)
        payload = json.loads(sent['messages'][1]['content'])
        if 'object' in payload:
            item = payload['object']
            result = {'object_id': item['id'], 'text': 'A green tree.' if item['id'] == 'tree' else 'One house.'}
        else:
            scene_calls.append(sent)
            text = rejected_draft if len(scene_calls) == 1 else 'One tree beside one house on neutral ground.'
            result = {'prompts': {'s': {'text': text, 'negative': 'writing, watermark',
                      'background_text': 'Neutral ground.', 'regions': []}}}
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'eval_count': 20,
            'message': {'content': json.dumps(result)}})
    transport = httpx.MockTransport(response)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: actual_client(transport=transport, **kwargs))
    adapter = OllamaAdapter()
    result = adapter.prompts([slot], '统一风格，不在图片内生成文字', 'fake-text')
    assert len(requests) == 4 and len(scene_calls) == 2
    assert scene_calls[0]['messages'][1] == scene_calls[1]['messages'][1]
    assert scene_calls[0]['format'] == scene_calls[1]['format']
    assert '不在图片内生成文字' in scene_calls[1]['messages'][1]['content']
    from backend.app.community_prompts import REPAIR
    assert REPAIR in scene_calls[1]['messages'][0]['content']
    assert all('REJECTED_DRAFT_BODY' not in json.dumps(sent) for sent in requests)
    assert result['s']['text'] == 'One tree beside one house on neutral ground.'
    assert result['s']['negative'] == 'writing, watermark'
    assert result['s']['regions'] == [{'object_id': 'tree', 'text': 'A green tree.'}, {'object_id': 'building', 'text': 'One house.'}]
    evidence = adapter.last_prompt_evidence
    assert [item['phase'] for item in evidence] == ['scene', 'scene_repair', 'object', 'object']
    assert evidence[0]['input_sha256'] == evidence[1]['input_sha256']
    assert evidence[0]['fixed_object_placements'] == evidence[1]['fixed_object_placements']
    assert all(item['done'] is True and item['eval_count'] == 20 for item in evidence)
    assert evidence[1]['result_valid'] is True


@pytest.mark.parametrize('bad_value', ['', None, True, '中文 no text', 'x' * 5501])
def test_invalid_english_scene_does_not_qualify_for_exclusion_repair(bad_value):
    from backend.app.models import ModelError
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.calls = getattr(self, 'calls', 0) + 1
            return {'prompts': {'s': {'text': bad_value, 'negative': 'text', 'background_text': 'Neutral ground.', 'regions': []}}}
    adapter = Adapter()
    with pytest.raises(ModelError):
        adapter.prompts([{'id': 's', 'description': '批准画面', 'director': configured()}], '统一风格', 'fake-text')
    assert adapter.calls == 1 and [item['phase'] for item in adapter.last_prompt_evidence] == ['scene']


@pytest.mark.parametrize('clause,noun', [('No text.', 'text'), ('without text.', 'text'),
    ('No readable text.', 'readable text'), ('Without any writing.', 'writing'), ('No lettering.', 'lettering')])
def test_authorized_complete_text_ban_clauses_move_to_real_negative_without_changing_scene(clause, noun):
    from backend.app.models import DEFAULT_STYLE
    slot = {'id': 's', 'description': '一个红杯', 'director': default_director()}
    value = {'text': 'One red ceramic cup. ' + clause, 'background_text': 'A white tabletop.',
             'negative': 'watermark, distortion'}
    clean, nouns, fields = OllamaAdapter.normalize_text_exclusions(value, slot, DEFAULT_STYLE)
    assert clean['text'] == 'One red ceramic cup.' and clean['background_text'] == value['background_text']
    assert clean['negative'] == 'watermark, distortion, ' + noun
    assert nouns == {noun} and fields == ['text']
    assert value['text'].endswith(clause) and value['negative'] == 'watermark, distortion'
    assert OllamaAdapter.normalize_text_exclusions(clean, slot, DEFAULT_STYLE) == (clean, set(), [])


@pytest.mark.parametrize('source_field', ['style', 'description', 'notes', 'object_description'])
def test_text_clause_migration_requires_explicit_approved_source_field(source_field):
    slot = {'id': 's', 'description': '红杯', 'director': {**default_director(), 'objects': [
        {'id': 'cup', 'label': '杯子', 'description': '红色陶瓷', 'x': 8, 'y': 25,
         'width': 36, 'height': 55, 'depth': 'midground'}]}}
    style = '克制插画'
    if source_field == 'style':
        style += '，不在图片内生成文字'
    elif source_field == 'description':
        slot['description'] += '，不要文字'
    elif source_field == 'notes':
        slot['director']['notes'] = '禁止生成文字'
    else:
        slot['director']['objects'][0]['description'] += '，无文字'
    value = {'text': 'One red cup, no text, a white surface.', 'background_text': 'White tabletop.', 'negative': ''}
    clean, nouns, fields = OllamaAdapter.normalize_text_exclusions(value, slot, style)
    assert clean['text'] == 'One red cup, a white surface.' and clean['negative'] == 'text'
    assert nouns == {'text'} and fields == ['text']


@pytest.mark.parametrize('bad_clause', ['No shadow.', 'Without a handle.', 'A cup with no text.',
    'No text or labels.', 'A cup without text on its side.', 'No text on the cup.'])
def test_text_migration_does_not_remove_unknown_partial_or_sentence_internal_negation(bad_clause):
    from backend.app.models import DEFAULT_STYLE
    slot = {'id': 's', 'description': '红杯'}
    value = {'text': 'One red cup. ' + bad_clause, 'background_text': 'White tabletop.', 'negative': 'distortion'}
    assert OllamaAdapter.normalize_text_exclusions(value, slot, DEFAULT_STYLE) == (value, set(), [])


@pytest.mark.parametrize('source', ['克制插画', 'Print "No text" on the book.', '写上“不要文字”这句话', 'No words.'])
def test_no_authorized_corresponding_source_keeps_complete_text_ban_for_rejection(source):
    slot = {'id': 's', 'description': '红杯', 'director': default_director()}
    value = {'text': 'One red cup. No text.', 'background_text': 'White tabletop.', 'negative': 'distortion'}
    assert OllamaAdapter.normalize_text_exclusions(value, slot, source) == (value, set(), [])


@pytest.mark.parametrize('negative', [None, True, {}, '中文', 'x' * 3000])
def test_text_ban_cannot_be_silently_removed_when_negative_is_invalid_or_full(negative):
    from backend.app.models import DEFAULT_STYLE, ModelError
    value = {'text': 'One red cup. No text.', 'background_text': 'White tabletop.', 'negative': negative}
    with pytest.raises(ModelError):
        OllamaAdapter.normalize_text_exclusions(value, {'id': 's', 'description': '红杯'}, DEFAULT_STYLE)


def test_migrated_text_ban_is_preserved_in_negative_after_one_other_exclusion_repair():
    from backend.app.models import DEFAULT_STYLE
    class Adapter(OllamaAdapter):
        scene_calls = 0
        def _chat(self, model, system, payload, output_format='json'):
            if 'object' in payload:
                return {'object_id': payload['object']['id'], 'text': 'One red cup.'}
            self.scene_calls += 1
            text = 'One red cup. No text. No shadow.' if self.scene_calls == 1 else 'One red cup on a white tabletop.'
            return {'prompts': {'s': {'text': text, 'background_text': 'White tabletop.', 'negative': '', 'regions': []}}}
    slot = {'id': 's', 'description': '红杯', 'director': {**default_director(), 'objects': [
        {'id': 'cup', 'label': '杯子', 'description': '红色陶瓷', 'x': 8, 'y': 25,
         'width': 36, 'height': 55, 'depth': 'midground'}]}}
    adapter = Adapter()
    result = adapter.prompts([slot], DEFAULT_STYLE, 'fake-text')
    assert adapter.scene_calls == 2 and result['s']['negative'] == 'text'
    assert result['s']['text'] == 'One red cup on a white tabletop.'
    assert adapter.last_prompt_evidence[1]['text_exclusion_normalization']['negative_nouns'] == ['text']


@pytest.mark.parametrize('clause', ['No text in the image.', 'without text in pictures.',
    'No readable text in a frame.', 'No text in cup.', 'No text in the image with a cup.',
    '"No text in the image".', 'No text in the image or on a cup.'])
def test_text_exclusion_image_scope_is_exact_and_preserves_extra_object_facts(clause):
    from backend.app.models import DEFAULT_STYLE
    value = {'text': 'One red cup. ' + clause + ' A blue book.', 'background_text': 'White tabletop.', 'negative': 'distortion'}
    clean, nouns, fields = OllamaAdapter.normalize_text_exclusions(value, {'id': 's', 'description': '红杯与蓝书'}, DEFAULT_STYLE)
    if clause in ('No text in the image.', 'without text in pictures.'):
        assert clean['text'] == 'One red cup. A blue book.'
        assert clean['negative'] == 'distortion, text' and nouns == {'text'} and fields == ['text']
    else:
        assert clean == value and nouns == set() and fields == []


def test_actual_known_image_scoped_text_ban_routes_before_object_translation_without_repair():
    from backend.app.models import DEFAULT_STYLE
    class Adapter(OllamaAdapter):
        calls = 0
        def _chat(self, model, system, payload, output_format='json'):
            self.calls += 1
            if 'object' in payload:
                item = payload['object']
                return {'object_id': item['id'], 'text': 'A green tree.' if item['id'] == 'tree' else 'One house.'}
            return {'prompts': {'s': {'text': 'One tree beside one house. No text in the image.',
                'background_text': 'Neutral ground.', 'negative': 'watermark', 'regions': []}}}
    adapter = Adapter()
    result = adapter.prompts([{'id': 's', 'description': '树木与房子', 'director': configured()}], DEFAULT_STYLE, 'fake-text')
    assert adapter.calls == 3 and result['s']['text'] == 'One tree beside one house.'
    assert result['s']['negative'] == 'watermark, text'
    assert [item['phase'] for item in adapter.last_prompt_evidence] == ['scene', 'object', 'object']


def test_region_health_missing_node_is_reported_separately_from_normal_generation():
    adapter = ComfyAdapter()
    def get(path, timeout=15):
        if path.endswith('CheckpointLoaderSimple'):
            return {'CheckpointLoaderSimple': {'input': {'required': {'ckpt_name': [['test-checkpoint']]}}}}
        if path.endswith('ConditioningSetArea'):
            return {'ConditioningSetArea': {'input': {}}}
        return {}
    adapter._get = get
    health = adapter.health()
    assert health['available'] is True
    assert health['region_control']['available'] is False and health['region_control']['error']


@pytest.mark.parametrize('status,payload,known_rejection', [
    (400, {'error': {'type': 'invalid_prompt', 'message': 'missing node'}, 'node_errors': {}}, True),
    (500, {'error': {'type': 'internal_error', 'message': 'unknown'}, 'node_errors': {}}, False),
    (400, {'error': {'message': 'conflicting id'}, 'node_errors': {}, 'prompt_id': 'possibly-accepted'}, False),
    (400, {'error': 'proxy rejection'}, False),
])
def test_comfy_submission_only_exact_validation_rejection_is_known_unaccepted(monkeypatch, status, payload, known_rejection):
    import httpx
    from backend.app.models import RejectedSubmissionError
    actual_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: actual_client(transport=transport, **kwargs))
    adapter = ComfyAdapter()
    with pytest.raises(RejectedSubmissionError if known_rejection else httpx.HTTPStatusError):
        adapter.submit('test-checkpoint', 'Scene.', 'text', 1, 'square', 'test-key')


def test_local_workflow_failure_is_known_unsubmitted_but_transport_timeout_remains_unknown(monkeypatch):
    import httpx
    from backend.app.models import RejectedSubmissionError
    actual_client = httpx.Client
    requests = []
    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout('unknown accepted state', request=request)
    transport = httpx.MockTransport(timeout)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: actual_client(transport=transport, **kwargs))
    adapter = ComfyAdapter()
    with pytest.raises(RejectedSubmissionError):
        adapter.submit('test-checkpoint', 'Scene.', 'text', 1, 'invalid-aspect', 'test-key')
    assert requests == []
    with pytest.raises(httpx.ReadTimeout):
        adapter.submit('test-checkpoint', 'Scene.', 'text', 1, 'square', 'test-key')
    assert len(requests) == 1
