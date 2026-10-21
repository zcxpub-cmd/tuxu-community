"""Frozen masked conditioning boundaries, without inference or an actual Store."""
import copy

import pytest
from fastapi import HTTPException
from PIL import Image, ImageChops, ImageDraw

from backend.app.director import MASKED_REGIONS_MODE, compile_controls, default_director, region_areas
from backend.app.main import digest, prompt_hash, validate_prompts
from backend.app.models import ComfyAdapter, RejectedSubmissionError
from backend.app.privacy import public_task


def scene(aspect='landscape'):
    config = {**default_director(), 'style_id': 'three_d', 'brightness': 'high', 'objects': [
        {'id': 'cup', 'label': '红杯', 'description': '一个红色陶瓷杯', 'x': 8, 'y': 25,
         'width': 36, 'height': 55, 'depth': 'midground'},
        {'id': 'book', 'label': '蓝书', 'description': '一本闭合的蓝色书', 'x': 56, 'y': 38,
         'width': 36, 'height': 40, 'depth': 'midground'}]}
    slot = {'id': 's', 'aspect_ratio': aspect, 'director': config}
    value = {'text': 'One red cup and one closed blue book on a white table.', 'negative': 'text',
             'background_text': 'A white tabletop in soft light.', 'regions': [
                 {'object_id': 'cup', 'text': 'One red ceramic cup.'},
                 {'object_id': 'book', 'text': 'One closed blue book.'}]}
    return slot, {'s': value}


def test_masked_compile_is_explicit_idempotent_and_legacy_default_is_unchanged():
    slot, raw = scene()
    legacy = compile_controls(raw, [slot])
    assert 'conditioning_mode' not in legacy['s'] and legacy['s']['global_text'] == legacy['s']['text']
    masked = compile_controls(raw, [slot], conditioning_mode=MASKED_REGIONS_MODE)
    assert masked['s']['conditioning_mode'] == MASKED_REGIONS_MODE
    assert masked['s']['global_text'] == masked['s']['background_text'] != masked['s']['text']
    assert masked['s']['regions'] == legacy['s']['regions']
    assert compile_controls(masked, [slot], conditioning_mode=MASKED_REGIONS_MODE) == masked
    assert compile_controls(masked, [slot]) == masked
    assert raw['s']['text'].startswith('One red cup') and 'global_text' not in raw['s']
    ordinary = {'old': {'text': 'An unchanged old scene.', 'negative': 'text'}}
    assert compile_controls(ordinary, [{'id': 'old'}], conditioning_mode=MASKED_REGIONS_MODE) == ordinary


def test_mode_enters_hash_only_when_present_and_validation_keeps_frozen_background():
    slot, raw = scene()
    legacy = compile_controls(raw, [slot])
    assert prompt_hash({'prompts': legacy}) == digest(legacy)
    masked = compile_controls(raw, [slot], conditioning_mode=MASKED_REGIONS_MODE)
    assert validate_prompts(masked, [slot], english=True, require_background=True) == masked
    changed = copy.deepcopy(masked)
    changed['s'].pop('conditioning_mode')
    assert prompt_hash({'prompts': changed}) != prompt_hash({'prompts': masked})
    changed = copy.deepcopy(masked)
    changed['s']['global_text'] = changed['s']['text']
    with pytest.raises(HTTPException):
        validate_prompts(changed, [slot], english=True)


@pytest.mark.parametrize('mode', [None, '', 'mask bounds', 'exclusive_masks_v1', True, [], {}])
def test_present_unknown_or_empty_mode_is_rejected(mode):
    slot, raw = scene()
    raw['s']['global_text'] = raw['s']['background_text']
    raw['s']['conditioning_mode'] = mode
    with pytest.raises(HTTPException):
        validate_prompts(raw, [slot], english=True)


@pytest.mark.parametrize('missing', ['objects', 'background_text', 'global_text'])
def test_mask_mode_requires_objects_and_both_frozen_background_fields(missing):
    slot, raw = scene()
    raw['s']['global_text'] = raw['s']['background_text']
    raw['s']['conditioning_mode'] = MASKED_REGIONS_MODE
    if missing == 'objects':
        slot['director']['objects'] = []
        raw['s']['regions'] = []
        raw['s'].pop('background_text')
        raw['s'].pop('global_text')
    else:
        raw['s'].pop(missing)
    with pytest.raises(HTTPException):
        validate_prompts(raw, [slot], english=True)


def mask_pixels(graph, reference, cache):
    """Independently interpret only native Solid/Composite mask pixel operations."""
    node_id = reference[0]
    if node_id in cache:
        return cache[node_id]
    node = graph[node_id]
    value = node['inputs']
    if node['class_type'] == 'SolidMask':
        result = Image.new('L', (value['width'], value['height']), round(value['value'] * 255))
    elif node['class_type'] == 'MaskComposite':
        destination = mask_pixels(graph, value['destination'], cache)
        source = mask_pixels(graph, value['source'], cache)
        placed = Image.new('L', destination.size)
        placed.paste(source, (value['x'], value['y']))
        if value['operation'] == 'add':
            result = ImageChops.add(destination, placed)
        elif value['operation'] == 'subtract':
            result = ImageChops.subtract(destination, placed)
        else:
            raise AssertionError('Unexpected operator in frozen mask graph')
    else:
        raise AssertionError('Non-mask node in mask reference')
    cache[node_id] = result
    return result


@pytest.mark.parametrize('aspect', ['landscape', 'square', 'portrait'])
@pytest.mark.parametrize('layout', ['separate', 'overlap', 'full_frame'])
def test_full_canvas_masks_are_rectangles_and_background_is_union_complement(aspect, layout):
    slot, raw = scene(aspect)
    if layout == 'overlap':
        slot['director']['objects'][0].update(x=20, y=20, width=60, height=60)
        slot['director']['objects'][1].update(x=45, y=45, width=45, height=45)
    elif layout == 'full_frame':
        slot['director']['objects'][0].update(x=0, y=0, width=100, height=100)
    prompt = compile_controls(raw, [slot], conditioning_mode=MASKED_REGIONS_MODE)['s']
    areas = region_areas(slot, prompt)
    graph = ComfyAdapter.workflow('checkpoint', prompt['global_text'], prompt['negative'], 7,
                                  aspect, areas, conditioning_mode=MASKED_REGIONS_MODE)
    assert len(graph) == 22 and graph['5']['inputs']['positive'] == ['22', 0]
    assert not any(node['class_type'] == 'ConditioningSetArea' for node in graph.values())
    cache, masks = {}, []
    size = (graph['4']['inputs']['width'], graph['4']['inputs']['height'])
    union = Image.new('L', size)
    for index, area in enumerate(areas):
        encode, condition = str(13 + 5 * index), str(14 + 5 * index)
        assert graph[encode]['inputs']['text'] == area['text']
        assert graph[condition]['inputs']['set_cond_area'] == 'default'
        actual = mask_pixels(graph, graph[condition]['inputs']['mask'], cache)
        expected = Image.new('L', size)
        ImageDraw.Draw(expected).rectangle((area['x'], area['y'], area['x'] + area['width'] - 1,
                                           area['y'] + area['height'] - 1), fill=255)
        assert actual.size == size and actual.tobytes() == expected.tobytes()
        union = ImageChops.lighter(union, expected)
        masks.append(actual)
    assert graph['20']['inputs']['set_cond_area'] == 'default'
    background = mask_pixels(graph, graph['20']['inputs']['mask'], cache)
    assert background.tobytes() == ImageChops.invert(union).tobytes()
    if layout == 'overlap':
        assert ImageChops.multiply(*masks).getbbox() is not None
    if layout == 'full_frame':
        assert background.getbbox() is None


def test_legacy_7_and_13_node_graphs_keep_original_inputs_without_any_mask_nodes():
    slot, raw = scene()
    prompt = compile_controls(raw, [slot])['s']
    areas = region_areas(slot, prompt)
    plain = ComfyAdapter.workflow('checkpoint', ' Legacy scene.\n', 'negative', 17, 'landscape')
    regional = ComfyAdapter.workflow('checkpoint', ' Legacy scene.\n', 'negative', 17, 'landscape', areas)
    assert len(plain) == 7 and len(regional) == 13
    assert plain['2']['inputs']['text'] == regional['2']['inputs']['text'] == ' Legacy scene.\n'
    assert regional['8']['inputs']['text'] == areas[0]['text'] and regional['11']['inputs']['text'] == areas[1]['text']
    assert all(node['class_type'] not in ('SolidMask', 'MaskComposite', 'ConditioningSetMask') for node in regional.values())
    assert ComfyAdapter.workflow('checkpoint', ' Legacy scene.\n', 'negative', 17, 'landscape', areas, None) == regional


def test_mask_node_health_is_independent_of_legacy_area_node_health():
    adapter = ComfyAdapter()
    def get(path, timeout=15):
        name = path.rsplit('/', 1)[-1]
        if name == 'CheckpointLoaderSimple':
            return {name: {'input': {'required': {'ckpt_name': [['checkpoint']]}}}}
        return {} if name == 'ConditioningSetArea' else {name: {'input': {}}}
    adapter._get = get
    health = adapter.health()
    assert health['region_control']['available'] is False and health['mask_control']['available'] is True
    original_get = get
    adapter._get = lambda path, timeout=15: {} if path.endswith('ConditioningSetMask') else original_get(path, timeout)
    assert adapter.health()['mask_control']['available'] is False


def test_invalid_mask_mode_or_empty_regions_is_known_unsubmitted_without_network(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('Invalid graph attempted HTTP'))
    adapter = ComfyAdapter()
    for mode, regions in [('unknown', None), (MASKED_REGIONS_MODE, [])]:
        with pytest.raises(RejectedSubmissionError):
            adapter.submit('checkpoint', 'background', 'negative', 7, 'square', 'key', regions, mode)


def test_conditioning_mode_is_private_at_every_nested_json_surface():
    mode = MASKED_REGIONS_MODE
    task = {'id': 'task', 'conditioning_mode': mode, 'plan': {'slots': [
        {'id': 's', 'description': '简单画面', 'conditioning_mode': mode, 'extra': [{'conditioning_mode': mode}]}]},
        'approval': {'conditioning_mode': mode, 'snapshot': {'conditioning_mode': mode}},
        'jobs': [{'status': 'completed', 'conditioning_mode': mode, 'snapshot': {'conditioning_mode': mode},
                  'slots': [{'conditioning_mode': mode}], 'submissions': [{'conditioning_mode': mode}]}],
        'assets': [{'conditioning_mode': mode, 'processing': {'conditioning_mode': mode},
                    'region_areas': [{'conditioning_mode': mode, 'x': 8}]}]}
    before = copy.deepcopy(task)
    visible = public_task(task)
    import json
    assert 'conditioning_mode' not in json.dumps(visible) and mode not in json.dumps(visible)
    assert task == before
