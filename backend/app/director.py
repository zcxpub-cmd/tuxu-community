"""Public basic style, lighting and box-placement mappings for Community."""
from __future__ import annotations

import copy
import math

STYLES = {
    'inherit': '',
    'editorial': 'illustration',
    'photographic': 'photograph',
    'watercolor': 'watercolor',
    'ink': 'ink',
    'flat': 'vector',
    'cinematic': 'cinematic',
    'three_d': '3D illustration',
    'diagram': 'diagram',
}
BRIGHTNESS = {
    'low': 'dark',
    'balanced': 'balanced brightness',
    'high': 'bright',
}
LIGHTING = {
    'soft': 'soft light',
    'hard': 'direct light',
    'backlit': 'back light',
}
CAMERAS = {
    'wide': 'wide shot',
    'medium': 'medium shot',
    'closeup': 'close up',
    'topdown': 'overhead view',
    'isometric': 'isometric',
}
DEPTHS = ('foreground', 'midground', 'background')
SIZES = {'landscape': (1152, 768), 'square': (1024, 1024), 'portrait': (768, 1152)}
MASKED_REGIONS_MODE = 'masked_regions_v1'


def default_director():
    return {'schema_version': 1, 'style_id': 'inherit', 'brightness': 'balanced',
            'lighting': 'soft', 'camera': 'medium', 'notes': '', 'objects': []}


def validate_director(value):
    if not isinstance(value, dict):
        raise ValueError('导演配置必须为对象。')
    fields = {'schema_version', 'style_id', 'brightness', 'lighting', 'camera', 'notes', 'objects', 'background'}
    if set(value) - fields:
        raise ValueError('导演配置包含未知字段。')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('导演配置版本无效。')
    result = {}
    for field, options in (('style_id', STYLES), ('brightness', BRIGHTNESS), ('lighting', LIGHTING), ('camera', CAMERAS)):
        if not isinstance(value.get(field), str) or value[field] not in options:
            raise ValueError('导演配置中的风格、明暗、光照或镜头选项无效。')
        result[field] = value[field]
    notes = value.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 600:
        raise ValueError('导演补充说明必须为最多 600 字的文本。')
    objects = value.get('objects')
    if not isinstance(objects, list) or len(objects) > 4:
        raise ValueError('导演台最多支持 4 个画面物体。')
    result = {'schema_version': 1, **result, 'notes': notes.strip(), 'objects': []}
    if 'background' in value:
        background = value['background']
        if not isinstance(background, str) or len(background) > 300:
            raise ValueError('导演背景必须为最多 300 字的文本。')
        result['background'] = background.strip()
        if result['background'] and not objects:
            raise ValueError('非空导演背景需要至少一个画面物体。')
    ids = set()
    for item in objects:
        if not isinstance(item, dict) or set(item) - {'id', 'label', 'description', 'x', 'y', 'width', 'height', 'depth'}:
            raise ValueError('导演物体格式无效或包含未知字段。')
        clean = {}
        for field, limit in (('id', 80), ('label', 80), ('description', 300)):
            text = item.get(field, '')
            if not isinstance(text, str) or len(text) > limit or (field != 'description' and not text.strip()):
                raise ValueError('物体 ID、名称或细节说明无效或过长。')
            clean[field] = text.strip()
        if clean['id'] in ids:
            raise ValueError('导演物体 ID 不能重复。')
        ids.add(clean['id'])
        for field in ('x', 'y', 'width', 'height'):
            coordinate = item.get(field)
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate):
                raise ValueError('物体区域坐标必须是有限数值。')
            clean[field] = coordinate
        if not (0 <= clean['x'] <= 100 and 0 <= clean['y'] <= 100 and
                8 <= clean['width'] <= 100 and 8 <= clean['height'] <= 100 and
                clean['x'] + clean['width'] <= 100 + 1e-9 and clean['y'] + clean['height'] <= 100 + 1e-9):
            raise ValueError('物体区域必须完全在画面内，宽高须为 8—100%。')
        if item.get('depth') not in DEPTHS:
            raise ValueError('物体景深选项无效。')
        clean['depth'] = item['depth']
        result['objects'].append(clean)
    return result


def control_text(director):
    values = [STYLES[director['style_id']], BRIGHTNESS[director['brightness']],
              LIGHTING[director['lighting']], CAMERAS[director['camera']]]
    return ', '.join(value for value in values if value) + '.'


def placement_text(item):
    column = min(2, int((item['x'] + item['width'] / 2) / (100 / 3)))
    row = min(2, int((item['y'] + item['height'] / 2) / (100 / 3)))
    horizontal, vertical = ('left', 'center', 'right')[column], ('upper', 'middle', 'lower')[row]
    name = 'center' if row == column == 1 else vertical + '-' + horizontal
    return f'{name} area'


def _compiled_line(line):
    """Recognize only reserved old markers or the exact short lines we append."""
    if line.startswith(('Director controls:', 'Region placement:')):
        return True
    words = line.removesuffix('.').split(', ')
    if len(words) == 4 and words[0] in STYLES.values():
        words = words[1:]
    if (len(words) == 3 and words[0] in BRIGHTNESS.values() and
            words[1] in LIGHTING.values() and words[2] in CAMERAS.values()):
        return True
    positions = {vertical + '-' + horizontal + ' area' for vertical in ('upper', 'middle', 'lower')
                 for horizontal in ('left', 'center', 'right')} | {'center area'}
    return len(words) == 2 and words[0] in positions and words[1] in DEPTHS


def _scene_body(text):
    return '\n'.join(line for line in text.splitlines() if not _compiled_line(line)).strip()


def _region_body(text):
    """Keep user text literal in Comfy's parenthesis-aware parser, without weights."""
    body = _scene_body(text).replace('\\(', '(').replace('\\)', ')')
    return body.replace('(', '\\(').replace(')', '\\)')


def generation_text(prompt, regional=False):
    """Select the already-approved input, including byte-preserving legacy fallbacks."""
    if regional:
        return prompt.get('global_text', prompt.get('background_text', prompt['text']))
    return prompt['text']


def compile_controls(prompts, slots, conditioning_mode=None):
    """Every fixed choice reaches the approved English prompts, even if the LLM omits it."""
    if conditioning_mode not in (None, MASKED_REGIONS_MODE):
        raise ValueError('区域提示词编译模式无效。')
    result = copy.deepcopy(prompts)
    for slot in slots:
        if not slot.get('director'):
            continue
        director = slot['director']
        suffix = control_text(director)
        prompt = result[slot['id']]
        # Reserved internal controls are compiled and hashed before approval, never at submit time.
        prompt['text'] = _scene_body(prompt['text']) + '\n' + suffix
        if 'background_text' in prompt:
            prompt['background_text'] = _scene_body(prompt['background_text']) + '\n' + suffix
        objects = {item['id']: item for item in director['objects']}
        if objects:
            mode = conditioning_mode if conditioning_mode is not None else prompt.get('conditioning_mode')
            if mode not in (None, MASKED_REGIONS_MODE):
                raise ValueError('区域提示词编译模式无效。')
            if mode == MASKED_REGIONS_MODE:
                if not isinstance(prompt.get('background_text'), str) or not prompt['background_text'].strip():
                    raise ValueError('分区提示词编译需要已转换的背景。')
                prompt['conditioning_mode'] = mode
                prompt['global_text'] = prompt['background_text']
            else:
                # Default compilation retains the prior full-scene contract.
                prompt['global_text'] = prompt['text']
        elif 'conditioning_mode' in prompt:
            raise ValueError('无物体区域的提示词不能使用分区模式。')
        for region in prompt.get('regions', []):
            depth = objects[region['object_id']]['depth']
            region['text'] = (_region_body(region['text']) + '\n' +
                              placement_text(objects[region['object_id']]) + ', ' + depth + '.\n' + suffix)
    return result


def region_areas(slot, prompt):
    """Convert percentage boxes into supported 8px areas without changing the saved plan."""
    director = slot.get('director')
    if not director or not director['objects']:
        return []
    width, height = SIZES[slot['aspect_ratio']]
    prompts = {region['object_id']: region['text'] for region in prompt['regions']}
    result = []
    for item in director['objects']:
        x = math.floor(item['x'] * width / 100 / 8) * 8
        y = math.floor(item['y'] * height / 100 / 8) * 8
        right = min(width, math.ceil((item['x'] + item['width']) * width / 100 / 8) * 8)
        bottom = min(height, math.ceil((item['y'] + item['height']) * height / 100 / 8) * 8)
        area_width = min(width, max(64, right - x))
        area_height = min(height, max(64, bottom - y))
        x, y = min(x, width - area_width), min(y, height - area_height)
        result.append({'object_id': item['id'], 'text': prompts[item['id']], 'x': x, 'y': y,
                       'width': area_width, 'height': area_height, 'strength': 1.0})
    return result
