"""Fresh recommendations and local prompt boundaries, without real models/Store."""
import copy

import pytest

from backend.app.director import STYLES, STYLE_GUIDANCE, default_director
from backend.app.models import ModelError, OllamaAdapter
from backend.app.style_recommendations import (
    apply_recommendation, recommendation_schema, rule_recommendation, validate_recommendation,
)


@pytest.mark.parametrize('value', [None, [], {'style_id': 'unknown', 'reason': '原因'},
    {'style_id': 'inherit', 'reason': '原因'}, {'style_id': 'scientific', 'reason': ''},
    {'style_id': 'scientific', 'reason': 'x' * 101},
    {'style_id': 'scientific', 'reason': '原因', 'source': 'cloud'},
    {'style_id': 'scientific', 'reason': '原因', 'prompt': 'must remain private'}])
def test_invalid_recommendations_cannot_enter_saved_metadata(value):
    assert validate_recommendation(value) is None


def test_recommendation_reason_does_not_echo_manuscript_or_prompt():
    result = validate_recommendation({'style_id': 'scientific', 'reason': 'private manuscript phrase'})
    assert result['source'] == 'local_model'
    assert 'private' not in result['reason']
    assert len(result['reason']) <= 100


@pytest.mark.parametrize('text,style', [
    ('展示花瓣与叶脉', 'botanical'), ('庭院与建筑立面', 'architectural'),
    ('已有统计与占比', 'infographic'), ('生产流程和步骤', 'diagram'),
    ('蛋白与分子机理', 'scientific'), ('传统剪纸作品', 'paper_cut'),
    ('古诗与书法水墨', 'ink'), ('柔和水彩场景', 'watercolor'),
    ('立体模型与体积结构', 'three_d'), ('扁平色块图标', 'flat'),
    ('自然光摄影场景', 'photographic'), ('情节中的人物关系', 'cinematic'),
    ('文章主题的编辑插画', 'editorial'),
])
def test_fallback_uses_scene_semantics_and_labels_the_source(text, style):
    result = rule_recommendation('report', text, '')
    assert result['style_id'] == style
    assert result['source'] == 'semantic_rules' and result['reason'].startswith('规则推荐：')


def test_same_genre_can_recommend_different_styles_from_corresponding_paragraph():
    assert rule_recommendation('academic', '植物标本与叶脉', '')['style_id'] == 'botanical'
    assert rule_recommendation('academic', '室内空间与建筑', '')['style_id'] == 'architectural'
    assert rule_recommendation('academic', '实验细胞机理', '')['style_id'] == 'scientific'


def test_fresh_annotation_does_not_mutate_old_slot_or_manual_director():
    slot = {'description': '植物标本', 'director': {**default_director(), 'style_id': 'watercolor'}}
    old = copy.deepcopy(slot)
    result = apply_recommendation(slot, 'academic', '叶脉')
    assert slot == old and 'style_recommendation' not in slot
    assert result['director'] == old['director']
    assert result['style_recommendation']['style_id'] == 'botanical'


def test_validated_category_takes_precedence_and_repeat_annotation_preserves_source():
    slot = {'description': '植物标本', 'style_recommendation': {
        'style_id': 'ink', 'reason': '对应段落适合墨色表达', 'source': 'semantic_rules'}}
    result = apply_recommendation(slot, 'academic', '叶脉')
    assert result['style_recommendation']['style_id'] == 'ink'
    assert result['style_recommendation']['source'] == 'semantic_rules'
    assert apply_recommendation(result, 'academic', '叶脉') == result


def test_local_analysis_schema_and_runtime_preserve_validated_recommendation():
    paragraph = {'id': 'p', 'kind': 'paragraph', 'text': '植物叶脉的形态清晰可见。'}
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.payload, self.schema = payload, output_format
            return {'summary': '自写样例', 'coverage': ['p'], 'slots': [{
                'paragraph_id': 'p', 'description': '一片叶子的细部', 'reason': '说明形态',
                'caption': '示意', 'risk': '生成示意', 'aspect_ratio': 'square',
                'style_recommendation': {'style_id': 'botanical', 'reason': '表达叶片形态', 'source': 'semantic_rules'}}]}
    adapter = Adapter()
    result = adapter.analyze({'paragraphs': [paragraph]}, 'academic', '清晰插画', 'mock')
    recommendation = result['slots'][0]['style_recommendation']
    assert recommendation['style_id'] == 'botanical' and recommendation['source'] == 'local_model'
    assert len(adapter.payload['style_catalog']) == 13
    assert adapter.schema['properties']['slots']['items']['properties']['style_recommendation'] == recommendation_schema()
    assert result['read_chunks'][0]['style_recommendation_evidence'] == [
        {'paragraph_id': 'p', 'style_id': 'botanical', 'source': 'local_model'}]


def test_unusable_model_category_gets_labelled_rule_recommendation():
    result = apply_recommendation({'description': '建筑空间', 'style_recommendation': {
        'style_id': 'unlisted-style', 'reason': '未知类别'}}, 'report', '室内结构')
    assert result['style_recommendation']['style_id'] == 'architectural'
    assert result['style_recommendation']['source'] == 'semantic_rules'


def test_analysis_failure_is_not_reported_as_rule_or_local_model_success():
    class Adapter(OllamaAdapter):
        def _chat(self, *args, **kwargs):
            raise ModelError('local unavailable')
    with pytest.raises(ModelError, match='local unavailable'):
        Adapter().analyze({'paragraphs': [{'id': 'p', 'kind': 'paragraph', 'text': '叶脉'}]},
                          'academic', '插画', 'mock')


def test_all_style_ids_have_local_model_guidance_and_scene_schema():
    assert set(STYLES) == set(STYLE_GUIDANCE) and len(STYLES) == 14
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.payload, self.schema = payload, output_format
            return {'prompts': {'s': {'text': 'One green leaf with visible veins.', 'negative': 'text, lettering'}}}
    adapter = Adapter()
    slot = {'id': 's', 'description': '一片绿叶', 'director': {**default_director(), 'style_id': 'botanical'}}
    result = adapter.prompts([slot], '清晰插画', 'mock')
    assert result['s']['text'].startswith('One green leaf')
    assert adapter.payload['slots'][0]['style_guidance'] == STYLE_GUIDANCE['botanical']
    assert set(adapter.schema['properties']['prompts']['properties']) == {'s'}
    assert adapter.last_prompt_evidence[0]['result_valid'] is True
    assert 'paragraphs' not in adapter.payload and 'document' not in adapter.payload


@pytest.mark.parametrize('value', [{'text': '中文', 'negative': ''},
    {'text': 'One leaf.', 'negative': '中文'}, {'text': 'One leaf.', 'negative': '', 'unapproved': 'scene'}])
def test_bad_scene_model_output_fails_instead_of_deterministic_prompt_fallback(value):
    class Adapter(OllamaAdapter):
        def _chat(self, *args, **kwargs):
            return {'prompts': {'s': value}}
    adapter = Adapter()
    with pytest.raises(ModelError):
        adapter.prompts([{'id': 's', 'description': '叶子'}], '插画', 'mock')
    assert adapter.last_prompt_evidence[0]['result_valid'] is False


def test_scene_request_failure_remains_failed_evidence():
    class Adapter(OllamaAdapter):
        def _chat(self, *args, **kwargs):
            raise ModelError('local model offline')
    adapter = Adapter()
    with pytest.raises(ModelError, match='local model offline'):
        adapter.prompts([{'id': 's', 'description': '叶子'}], '插画', 'mock')
    evidence = adapter.last_prompt_evidence[0]
    assert evidence['request_state'] == 'failed' and evidence['result_valid'] is False
