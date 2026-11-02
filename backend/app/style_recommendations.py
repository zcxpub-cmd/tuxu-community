"""Bounded fresh-analysis style hints; no network and no changes on task reads.

The model selects a visual category. Public reasons are original, fixed short
explanations so a model cannot put a manuscript passage or prompt into metadata.
Rules are only a labelled recommendation fallback, never a prompt generator.
"""
from __future__ import annotations

import copy
import re

from .director import STYLES, STYLE_GUIDANCE

STYLE_REASONS = {
    'editorial': '用清晰的视觉重点概括段落主题，适合文章阅读。',
    'photographic': '以自然光照和材质表达具体场景；生成画面仍需标为示意。',
    'watercolor': '以柔和色层和纸面质感表达自然或抒情场景。',
    'ink': '以墨色层次和留白表达文化意象及空间关系。',
    'flat': '以简洁色块突出概念，减少画面信息负担。',
    'cinematic': '以视角、光线和空间层次表达情节及人物关系。',
    'three_d': '以体积和材质帮助理解物体形态及空间层次。',
    'diagram': '以简明图式解释已有结构或关系，适合说明性段落。',
    'scientific': '以科学示意突出已知结构或机理，避免把想象当作测量。',
    'infographic': '以视觉层级组织已有信息，准确文字和数字交给文档排版。',
    'architectural': '以一致透视和空间层次突出建筑及场所关系。',
    'botanical': '以清晰轮廓突出植物形态，保留原文已知特征。',
    'paper_cut': '以纸层、轮廓和投影表达简洁的文化或手工主题。',
}
RECOMMENDABLE_STYLES = tuple(style for style in STYLES if style != 'inherit')


def recommendation_schema():
    return {'type': 'object', 'additionalProperties': False, 'required': ['style_id', 'reason'],
            'properties': {'style_id': {'type': 'string', 'enum': list(RECOMMENDABLE_STYLES)},
                           'reason': {'type': 'string', 'minLength': 1, 'maxLength': 100}}}


def style_catalog():
    return {style: STYLE_GUIDANCE[style] for style in RECOMMENDABLE_STYLES}


def validate_recommendation(value):
    """Return safe additive metadata or None; never add fields to old slots.

    A category is untrusted until checked against the public catalogue. Do not
    publish the model's free-form reason: use our short category explanation.
    """
    if not isinstance(value, dict) or set(value) - {'style_id', 'reason', 'source'}:
        return None
    style_id, reason = value.get('style_id'), value.get('reason')
    if (not isinstance(style_id, str) or style_id not in RECOMMENDABLE_STYLES or
            not isinstance(reason, str) or not reason.strip() or len(reason) > 100):
        return None
    source = value.get('source', 'local_model')
    if source not in ('local_model', 'semantic_rules'):
        return None
    prefix = '规则推荐：' if source == 'semantic_rules' else ''
    return {'style_id': style_id, 'reason': prefix + STYLE_REASONS[style_id], 'source': source}


# Original category cues, evaluated against the corresponding paragraph and
# planned scene. Genre breaks ties only; it cannot replace scene semantics.
_CUES = {
    'botanical': ('植物形态', '叶脉', '花瓣', '根系', '植物标本', '树叶', '植物', 'botanical', 'petal', 'leaf vein'),
    'architectural': ('建筑', '室内', '街区', '城市空间', '庭院', '房间', '立面', 'architecture', 'interior', 'floor plan'),
    'infographic': ('信息图', '占比', '百分比', '统计', '数据对比', '增长率', 'infographic', 'percentage', 'statistics'),
    'diagram': ('流程', '关系图', '示意图', '步骤', '工作原理', '剖面', 'flowchart', 'schematic', 'cross section'),
    'scientific': ('实验', '细胞', '分子', '蛋白', '粒子', '地质', '机理', '科学示意', 'cell', 'molecule', 'experiment'),
    'paper_cut': ('剪纸', '纸雕', '纸艺', '折纸', 'paper cut', 'papercut', 'paper craft'),
    'ink': ('水墨', '书法', '国画', '古诗', '山水意象', 'ink wash', 'calligraphy'),
    'watercolor': ('水彩', '抒情', '柔和插画', '童话', 'watercolor', 'watercolour'),
    'three_d': ('三维', '立体模型', '三维模型', '体积结构', '3d', 'three dimensional'),
    'flat': ('扁平', '图标', '色块', '概念图', 'flat design', 'icon'),
    'photographic': ('摄影', '纪实', '自然光', '现场场景', '照片', 'photograph', 'documentary'),
    'cinematic': ('电影', '情节', '人物关系', '追逐', 'cinematic', 'story scene'),
    'editorial': ('编辑插画', '文章主题', 'editorial'),
}


def _contains(text, cue):
    # English cues use word boundaries, so e.g. "cell" does not match "excellent".
    return bool(re.search(r'(?<![a-z])' + re.escape(cue) + r'(?![a-z])', text)) if cue.isascii() else cue in text


def rule_recommendation(genre, paragraph_text, description):
    paragraph = paragraph_text.casefold() if isinstance(paragraph_text, str) else ''
    scene = description.casefold() if isinstance(description, str) else ''
    scores = {style: sum(2 * _contains(paragraph, cue) + 3 * _contains(scene, cue) for cue in cues)
              for style, cues in _CUES.items()}
    highest = max(scores.values())
    if highest:
        style_id = max(scores, key=scores.get)
    else:
        style_id = {'academic': 'scientific', 'news': 'editorial',
                    'novel': 'cinematic', 'report': 'editorial'}.get(genre, 'editorial')
    return validate_recommendation({'style_id': style_id, 'reason': STYLE_REASONS[style_id], 'source': 'semantic_rules'})


def apply_recommendation(slot, genre, paragraph_text):
    """Annotate a newly analyzed slot copy only; leave any director untouched."""
    result = copy.deepcopy(slot)
    proposed = result.get('style_recommendation')
    recommendation = validate_recommendation(proposed)
    result['style_recommendation'] = recommendation or rule_recommendation(genre, paragraph_text, result.get('description', ''))
    return result
