"""Local-only model adapters. These classes do not start or install models."""
from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path

import httpx

from .director import MASKED_REGIONS_MODE, control_text, placement_text
from . import community_prompts as community

TEXT_MODEL = 'qwen2.5vl:7b'
DEFAULT_STYLE = '社区基础插画：清楚表达原文，无文字。'
_TEXT_EXCLUSION = re.compile(r'(?:no|without)\s+(?:any\s+)?(text|readable text|legible text|written text|writing|words|lettering)'
                             r'(?:\s+in (?:the )?(?:image|picture|frame)s?)?', re.IGNORECASE)
_APPROVED_CHINESE_TEXT_BAN = re.compile(
    r'(?:(?:不|不要|禁止|避免|不得)(?:在(?:图片|图像|画面|图)(?:内|中|上))?'
    r'(?:生成|添加|绘制|显示|出现|使用)(?:任何|可读)?(?:文字|文本|字样)|'
    r'(?:不要|禁止|无|不含|没有)(?:任何|可读)?(?:文字|文本|字样))(?=$|和|及|或|、)')


class ModelError(RuntimeError):
    pass


class CompletedRequestError(ModelError):
    """History positively confirms completion without a usable image; safe to reauthorize."""
    pass


class RejectedSubmissionError(ModelError):
    """Positive evidence the request was rejected before being accepted into Comfy's queue."""
    pass


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ModelError('本地文本模型没有返回有效 JSON，请重试或更换模型。') from exc


class OllamaAdapter:
    def __init__(self, url='http://127.0.0.1:11434'):
        self.url = url.rstrip('/')

    def health(self):
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                response = client.get(self.url + '/api/tags')
                response.raise_for_status()
                names = [m['name'] for m in response.json().get('models', [])]
            return {'available': bool(names), 'model': TEXT_MODEL if TEXT_MODEL in names else (names[0] if names else ''),
                    'models': names, 'error': None if names else '本地未列出文本模型。'}
        except Exception as exc:
            return {'available': False, 'model': TEXT_MODEL, 'error': 'Ollama 不可访问：' + str(exc)}

    def _chat(self, model, system, payload, output_format='json'):
        structured = isinstance(output_format, dict)
        if structured:
            system += '\nReturn only JSON conforming to this required schema: ' + json.dumps(output_format, ensure_ascii=False)
        with httpx.Client(timeout=600, trust_env=False) as client:
            response = client.post(self.url + '/api/chat', json={
                'model': model, 'stream': False, 'format': output_format, 'keep_alive': 0,
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                'options': {'temperature': 0 if structured else 0.15, 'num_predict': 6000, 'num_ctx': 8192}})
            response.raise_for_status()
            result = response.json()
            if not result.get('done') or result.get('done_reason') == 'length':
                raise ModelError('本地文本模型输出未完整结束或达到长度上限，拒绝保存截断结果。')
            parsed = parse_json(result.get('message', {}).get('content', ''))
            if not isinstance(parsed, dict):
                raise ModelError('本地文本模型必须返回 JSON 对象。')
            parsed['_model_evidence'] = {k: result.get(k) for k in (
                'model', 'done', 'done_reason', 'prompt_eval_count', 'eval_count', 'total_duration')}
            return parsed

    @staticmethod
    def chunks(paragraphs, maximum=2000):
        chunks, current, size, serialized_size = [], [], 0, 0
        for paragraph in paragraphs:
            text = paragraph.get('text', '')
            pieces = [text[i:i + maximum] for i in range(0, len(text), maximum)] or ['']
            for part_index, piece in enumerate(pieces):
                item = {k: paragraph.get(k) for k in ('id', 'kind', 'level', 'source_index')}
                item['insertable'] = paragraph.get('insertable', True)
                item.update(text=piece, part_index=part_index + 1, part_count=len(pieces))
                item_size = len(json.dumps(item, ensure_ascii=False))
                if current and (size + len(piece) > maximum or serialized_size + item_size > 4000 or any(p['id'] == paragraph['id'] for p in current)):
                    chunks.append(current)
                    current, size, serialized_size = [], 0, 0
                current.append(item)
                size += len(piece)
                serialized_size += item_size
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def analysis_schema(paragraphs):
        """Public output protocol; coverage remains independently checked below."""
        ids = [paragraph['id'] for paragraph in paragraphs]
        body_ids = [paragraph['id'] for paragraph in paragraphs
                    if paragraph['kind'] == 'paragraph' and paragraph.get('insertable', True)]
        properties = {key: {'type': 'string', 'maxLength': limit} for key, limit in
                      (('reason', 100), ('description', 300), ('caption', 100), ('risk', 100))}
        properties['description']['minLength'] = 1
        properties['paragraph_id'] = {'type': 'string', **({'enum': body_ids} if body_ids else {})}
        properties['aspect_ratio'] = {'type': 'string', 'enum': ['landscape', 'square', 'portrait']}
        return {'type': 'object', 'additionalProperties': False, 'required': ['summary', 'coverage', 'slots'],
                'properties': {
                    'summary': {'type': 'string', 'maxLength': 300},
                    'coverage': {'type': 'array', 'minItems': len(ids), 'maxItems': len(ids), 'uniqueItems': True,
                                 'items': {'type': 'string', 'enum': ids}},
                    'slots': {'type': 'array', 'maxItems': 3 if body_ids else 0,
                              'items': {'type': 'object', 'additionalProperties': False,
                                        'required': list(properties), 'properties': properties}}}}

    def analyze(self, document, genre, style, model):
        if len(style) > 1000:
            raise ModelError('统一视觉风格超过 1,000 字，请精简后再分析以保证上下文完整。')
        system = community.ANALYSIS
        chunks = self.chunks(document['paragraphs'])
        if len(chunks) > 64:
            raise ModelError('正文过度碎片化，超过 64 个有界阅读块；请合并短段后再分析。')
        summaries, candidates, evidence = [], [], []
        for index, chunk in enumerate(chunks):
            expected = [p['id'] for p in chunk]
            result = self._chat(model, system, {'genre': genre, 'style': style, 'chunk_index': index + 1,
                'chunk_count': len(chunks), 'expected_coverage': expected, 'paragraphs': chunk},
                output_format=self.analysis_schema(chunk))
            coverage = result.get('coverage')
            if (not isinstance(coverage, list) or len(coverage) != len(expected) or
                    not all(isinstance(item, str) for item in coverage) or set(coverage) != set(expected)):
                raise ModelError(f'第 {index + 1} 个真实阅读分块的段落覆盖校验失败。')
            slots = result.get('slots')
            if not isinstance(slots, list) or len(slots) > 3:
                raise ModelError('分块配图候选超过限制或格式无效。')
            body_ids = {p['id'] for p in chunk if p['kind'] == 'paragraph' and p.get('insertable', True)}
            for slot in slots:
                if not isinstance(slot, dict) or slot.get('paragraph_id') not in body_ids:
                    raise ModelError('分块候选引用了未读取正文或虚构段落。')
                if any(len(str(slot.get(field, ''))) > bound for field, bound in (
                        ('description', 600), ('reason', 200), ('caption', 200), ('risk', 200))):
                    raise ModelError('分块模型候选输出超过有界综合限制，请重试。')
                candidates.append({**slot, 'candidate_id': 'candidate-' + str(len(candidates))})
            summary = str(result.get('summary', ''))
            if len(summary) > 600:
                raise ModelError('分块总结超过有界综合限制，请重试。')
            summaries.append(summary)
            evidence.append({'chunk_index': index + 1, 'paragraph_ids': expected, 'coverage': coverage,
                'character_count': sum(len(p['text']) for p in chunk),
                'input_sha256': hashlib.sha256(json.dumps(chunk, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                'parts': [{'paragraph_id': p['id'], 'part_index': p['part_index'], 'part_count': p['part_count'],
                           'character_count': len(p['text'])} for p in chunk],
                'model_evidence': result.get('_model_evidence', {})})
        synthesis_evidence = None
        if len(chunks) == 1:
            chosen, summary = candidates, summaries[0]
        else:
            # Bound synthesis input by keeping one unchanged candidate per original paragraph.
            unique = {}
            for candidate in candidates:
                unique.setdefault(candidate['paragraph_id'], candidate)
            candidates = list(unique.values())
            if len(candidates) > 15:
                candidates = [candidates[round(i * (len(candidates) - 1) / 14)] for i in range(15)]
            summary_limit = max(1, 1500 // len(summaries))
            synthesis = self._chat(model,
                community.SYNTHESIS,
                {'genre': genre, 'style': style, 'chunk_summary_excerpts': [s[:summary_limit] for s in summaries],
                 'candidate_previews': [{'candidate_id': c['candidate_id'], 'paragraph_id': c['paragraph_id'],
                                        'description_excerpt': str(c.get('description', ''))[:200]} for c in candidates]})
            selected = synthesis.get('selected_candidate_ids')
            by_id = {c['candidate_id']: c for c in candidates}
            if not isinstance(selected, list) or len(selected) > 3 or len(set(selected)) != len(selected) or any(c not in by_id for c in selected):
                raise ModelError('全篇综合返回了未知或重复的候选 ID。')
            chosen, summary = [by_id[c] for c in selected], str(synthesis.get('summary', ''))
            synthesis_evidence = synthesis.get('_model_evidence', {})
        seen, slots = set(), []
        for candidate in chosen:
            if candidate['paragraph_id'] in seen:
                continue
            seen.add(candidate['paragraph_id'])
            slots.append({k: v for k, v in candidate.items() if k != 'candidate_id'})
        return {'summary': summary, 'coverage': [p['id'] for p in document['paragraphs']], 'slots': slots[:3],
                'read_chunks': evidence, 'coverage_evidence': {'input_character_count': sum(len(p['text']) for p in document['paragraphs']),
                    'read_character_count': sum(e['character_count'] for e in evidence), 'chunk_count': len(chunks),
                    'synthesis': synthesis_evidence, 'all_chunks_validated': True}}

    @staticmethod
    def regional_prompt_schema(slot):
        """One bounded structured response; semantic and duplicate-ID checks remain in the service."""
        object_ids = [item['id'] for item in slot['director']['objects']]
        value = {'type': 'object', 'additionalProperties': False,
                 'required': ['text', 'negative', 'background_text', 'regions'], 'properties': {
                     'text': {'type': 'string', 'minLength': 1, 'maxLength': 5500},
                     'negative': {'type': 'string', 'maxLength': 3000},
                     'background_text': {'type': 'string', 'minLength': 1, 'maxLength': 2500},
                     'regions': {'type': 'array', 'minItems': len(object_ids), 'maxItems': len(object_ids),
                                 'items': {'type': 'object', 'additionalProperties': False,
                                           'required': ['object_id', 'text'], 'properties': {
                                               'object_id': {'type': 'string', 'enum': object_ids},
                                               'text': {'type': 'string', 'minLength': 1, 'maxLength': 650}}}}}}
        return {'type': 'object', 'additionalProperties': False, 'required': ['prompts'], 'properties': {
            'prompts': {'type': 'object', 'additionalProperties': False, 'required': [slot['id']],
                        'properties': {slot['id']: value}}}}

    @staticmethod
    def object_prompt_schema(object_id):
        return {'type': 'object', 'additionalProperties': False,
                'required': ['object_id', 'text'], 'properties': {
                    'object_id': {'type': 'string', 'enum': [object_id]},
                    'text': {'type': 'string', 'minLength': 1, 'maxLength': 650}}}

    @staticmethod
    def background_prompt_schema(slot_id):
        return {'type': 'object', 'additionalProperties': False,
                'required': ['slot_id', 'text'], 'properties': {
                    'slot_id': {'type': 'string', 'enum': [slot_id]},
                    'text': {'type': 'string', 'minLength': 1, 'maxLength': 650}}}

    @staticmethod
    def translated_phrase(text, maximum):
        """Validate English before deciding whether a bounded semantic repair is possible."""
        if (not isinstance(text, str) or not text.strip() or len(text) > maximum or
                re.search('[\u4e00-\u9fff]', text) or not re.search('[a-zA-Z]', text)):
            raise ModelError('独立画面提示词未得到有效英文，请重新转换。')
        return text.strip()

    @staticmethod
    def positive_exclusion(text):
        return bool(re.search(r'\b(?:no|without|do\s+not|avoid|exclude|excluding)\b', text, re.IGNORECASE))

    @classmethod
    def positive_phrase(cls, text, maximum):
        """Reject incomplete translation or exclusion instructions before approval."""
        text = cls.translated_phrase(text, maximum)
        if cls.positive_exclusion(text):
            raise ModelError('正向画面提示词含排除指令，请重新转换为正向属性。')
        return text

    @staticmethod
    def approved_text_exclusion(slot, style):
        """Only explicit approved text bans authorize routing a known English exclusion."""
        director = slot.get('director', {})
        sources = [style, slot.get('description', ''), director.get('notes', '')]
        sources += [item.get('description', '') for item in director.get('objects', [])]
        authorized = set()
        generic_text_nouns = {'text', 'readable text', 'legible text', 'written text', 'writing', 'words', 'lettering'}
        for source in sources:
            if not isinstance(source, str):
                continue
            for clause in re.split(r'[,;.!?\n，；。！？]', source):
                clause = clause.strip()
                if _APPROVED_CHINESE_TEXT_BAN.match(clause):
                    authorized.update(generic_text_nouns)
                english = _TEXT_EXCLUSION.fullmatch(clause) or re.fullmatch(
                    r'(?:do not|never)\s+(?:generate|add|include|render|show)\s+(?:any\s+)?'
                    r'(text|writing|words|lettering)(?:\s+in (?:the )?(?:image|picture|frame)s?)?', clause, re.IGNORECASE)
                if english:
                    noun = english[1].casefold()
                    authorized.update({'text', 'readable text', 'legible text', 'written text'} if noun == 'text' else {noun})
        return authorized

    @staticmethod
    def add_negative_nouns(negative, nouns):
        if not nouns:
            return negative
        if not isinstance(negative, str) or len(negative) > 3000 or re.search('[\u4e00-\u9fff]', negative):
            raise ModelError('文字禁令需要有效英文排除提示词，拒绝丢失排除条件。')
        existing = {part.strip().casefold() for part in re.split(r'[,;\n]', negative)}
        additions = [noun for noun in sorted(nouns) if noun.casefold() not in existing]
        result = ', '.join([part for part in [negative.strip(), *additions] if part])
        if len(result) > 3000:
            raise ModelError('文字禁令迁移后的排除提示词超过长度限制。')
        return result

    @classmethod
    def normalize_text_exclusions(cls, value, slot, style):
        """Move only authorized, complete known text-ban clauses into real negative text."""
        authorized = cls.approved_text_exclusion(slot, style)
        if not authorized:
            return value, set(), []
        result, nouns, fields = dict(value), set(), []
        for field in ('text', 'background_text'):
            body = value[field]
            parts = re.split(r'([,.;!?]\s*|\n+)', body)
            retained, removed = [], []
            for index in range(0, len(parts), 2):
                clause = parts[index]
                delimiter = parts[index + 1] if index + 1 < len(parts) else ''
                match = _TEXT_EXCLUSION.fullmatch(clause.strip())
                if match and match[1].casefold() in authorized:
                    removed.append(match[1].casefold())
                elif clause.strip():
                    retained.append(clause + delimiter)
            if removed:
                result[field] = ''.join(retained).strip().rstrip(',;').rstrip()
                nouns.update(removed)
                fields.append(field)
        if nouns:
            result['negative'] = cls.add_negative_nouns(value.get('negative'), nouns)
        return result, nouns, fields

    def object_prompt(self, item, model, slot_id):
        system = community.OBJECT
        payload = {'object': {key: item[key] for key in ('id', 'label', 'description')}}
        evidence = {'slot_id': slot_id, 'phase': 'object', 'object_id': item['id'], 'request_state': 'requesting',
                    'result_valid': False}
        self.last_prompt_evidence.append(evidence)
        try:
            response = self._chat(model, system, payload, output_format=self.object_prompt_schema(item['id']))
        except Exception as exc:
            evidence.update(request_state='failed', error_type=type(exc).__name__)
            raise
        evidence.update(request_state='completed', **response.get('_model_evidence', {}))
        if set(response) - {'_model_evidence'} != {'object_id', 'text'} or response.get('object_id') != item['id']:
            evidence['result_valid'] = False
            raise ModelError('独立物体提示词转换遗漏、改变或包含未知物体 ID。')
        try:
            result = {'object_id': item['id'], 'text': self.positive_phrase(response['text'], 650)}
        except ModelError:
            evidence['result_valid'] = False
            raise
        evidence['result_valid'] = True
        return result

    def background_prompt(self, background, model, slot_id):
        if not isinstance(background, str) or not background.strip() or len(background) > 300:
            raise ModelError('独立背景提示词需要非空、最多 300 字的批准背景说明。')
        system = community.BACKGROUND
        payload = {'slot_id': slot_id, 'background': background}
        evidence = {'slot_id': slot_id, 'phase': 'background', 'request_state': 'requesting',
                    'result_valid': False,
                    'input_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        self.last_prompt_evidence.append(evidence)
        try:
            response = self._chat(model, system, payload, output_format=self.background_prompt_schema(slot_id))
        except Exception as exc:
            evidence.update(request_state='failed', error_type=type(exc).__name__)
            raise
        if not isinstance(response, dict):
            evidence.update(request_state='completed')
            raise ModelError('独立背景提示词转换未返回有效对象。')
        evidence.update(request_state='completed', **response.get('_model_evidence', {}))
        if set(response) - {'_model_evidence'} != {'slot_id', 'text'} or response.get('slot_id') != slot_id:
            raise ModelError('独立背景提示词转换遗漏、改变或包含未知图位 ID。')
        text = self.positive_phrase(response['text'], 650)
        evidence['result_valid'] = True
        return text

    def prompts(self, slots, style, model):
        system = community.SCENE
        # One slot per request keeps approved user descriptions bounded and releases Ollama after each call.
        prompts = {}
        self.last_prompt_evidence = []
        for slot in slots:
            if slot.get('director', {}).get('background') and not slot['director'].get('objects'):
                raise ModelError('非空导演背景提示词需要至少一个画面物体。')
            payload = {'style': style, 'slots': [{'id': slot['id'], 'description': slot['description']}]}
            if slot.get('director'):
                payload['slots'][0]['director'] = slot['director']
                payload['slots'][0]['fixed_visual_controls'] = control_text(slot['director'])
                if slot['director']['objects']:
                    payload['slots'][0]['fixed_object_placements'] = [
                        {'id': item['id'], 'label': item['label'], 'placement': placement_text(item)}
                        for item in slot['director']['objects']]
            if len(json.dumps(payload, ensure_ascii=False)) > 6500:
                raise ModelError('统一风格与单图描述过长，请精简后再转换正式提示词。')
            objects = slot.get('director', {}).get('objects', [])
            if objects:
                schema = self.regional_prompt_schema(slot)
                evidence = {'slot_id': slot['id'], 'phase': 'scene', 'request_state': 'requesting',
                    'result_valid': False, 'fixed_object_placements': payload['slots'][0]['fixed_object_placements'],
                    'input_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
                self.last_prompt_evidence.append(evidence)
                try:
                    response = self._chat(model, system, payload, output_format=schema)
                except Exception as exc:
                    evidence.update(request_state='failed', error_type=type(exc).__name__)
                    raise
                evidence.update(request_state='completed', **response.get('_model_evidence', {}))
            else:
                response = self._chat(model, system, payload)
                self.last_prompt_evidence.append({'slot_id': slot['id'], **response.get('_model_evidence', {})})
            values = response.get('prompts', {})
            if not isinstance(values, dict) or set(values) != {slot['id']}:
                raise ModelError('正式提示词转换返回未知或遗漏图位。')
            if objects:
                value = values[slot['id']]
                if not isinstance(value, dict):
                    raise ModelError('正式提示词转换没有返回有效画面对象。')
                # A user-supplied environment is translated in its own context once.
                # Shared background drafts, including repair drafts, are never a fallback.
                independent_background = None
                if slot['director'].get('background'):
                    independent_background = self.background_prompt(slot['director']['background'], model, slot['id'])
                    value['background_text'] = independent_background
                # Only valid English with exclusion syntax qualifies for one scene repair.
                # The rejected response is never appended to the request or persisted.
                text = self.translated_phrase(value.get('text'), 5500)
                background = self.translated_phrase(value.get('background_text'), 2500)
                value, migrated_nouns, normalized_fields = self.normalize_text_exclusions(value, slot, style)
                if migrated_nouns:
                    evidence['text_exclusion_normalization'] = {
                        'fields': normalized_fields, 'negative_nouns': sorted(migrated_nouns), 'source_explicit': True}
                text = self.translated_phrase(value.get('text'), 5500)
                background = self.translated_phrase(value.get('background_text'), 2500)
                if self.positive_exclusion(text) or self.positive_exclusion(background):
                    evidence['result_valid'] = False
                    repair_system = system + community.REPAIR
                    evidence = {'slot_id': slot['id'], 'phase': 'scene_repair', 'request_state': 'requesting', 'result_valid': False,
                        'fixed_object_placements': payload['slots'][0]['fixed_object_placements'],
                        'input_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
                    self.last_prompt_evidence.append(evidence)
                    try:
                        response = self._chat(model, repair_system, payload, output_format=schema)
                    except Exception as exc:
                        evidence.update(request_state='failed', error_type=type(exc).__name__)
                        raise
                    evidence.update(request_state='completed', **response.get('_model_evidence', {}))
                    values = response.get('prompts', {})
                    if not isinstance(values, dict) or set(values) != {slot['id']} or not isinstance(values[slot['id']], dict):
                        evidence['result_valid'] = False
                        raise ModelError('修正提示词转换返回未知、遗漏或无效图位。')
                    value = values[slot['id']]
                    if independent_background is not None:
                        value['background_text'] = independent_background
                    try:
                        self.translated_phrase(value.get('text'), 5500)
                        self.translated_phrase(value.get('background_text'), 2500)
                        value, repaired_nouns, normalized_fields = self.normalize_text_exclusions(value, slot, style)
                        if migrated_nouns or repaired_nouns:
                            all_nouns = migrated_nouns | repaired_nouns
                            value['negative'] = self.add_negative_nouns(value.get('negative'), all_nouns)
                            evidence['text_exclusion_normalization'] = {
                                'fields': normalized_fields, 'negative_nouns': sorted(all_nouns), 'source_explicit': True}
                        self.positive_phrase(value.get('text'), 5500)
                        self.positive_phrase(value.get('background_text'), 2500)
                    except ModelError:
                        evidence['result_valid'] = False
                        raise
                    evidence['result_valid'] = True
                value['text'] = self.positive_phrase(value.get('text'), 5500)
                value['background_text'] = self.positive_phrase(value.get('background_text'), 2500)
                evidence['result_valid'] = True
                # Shared-context region bodies are deliberately discarded, never a fallback.
                # Every replacement must succeed through a real isolated object request.
                value['regions'] = [self.object_prompt(item, model, slot['id']) for item in objects]
                values[slot['id']] = value
            prompts.update(values)
        return prompts


class ComfyAdapter:
    def __init__(self, url='http://127.0.0.1:8188', poll_seconds=2, result_timeout=3600):
        self.url = url.rstrip('/')
        self.poll_seconds = poll_seconds
        self.result_timeout = result_timeout

    def _get(self, path, timeout=15):
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(self.url + path)
            response.raise_for_status()
            return response.json()

    def health(self):
        try:
            self._get('/system_stats', 3)
            objects = self._get('/object_info/CheckpointLoaderSimple', 10)
            checkpoints = objects.get('CheckpointLoaderSimple', {}).get('input', {}).get('required', {}).get('ckpt_name', [[]])[0]
            try:
                area = self._get('/object_info/ConditioningSetArea', 3)
                combine = self._get('/object_info/ConditioningCombine', 3)
                region_available = bool(area.get('ConditioningSetArea') and combine.get('ConditioningCombine'))
                region_error = None if region_available else '缺少 ConditioningSetArea 或 ConditioningCombine 标准节点。'
            except Exception as exc:
                region_available, region_error = False, '区域控制节点不可用：' + str(exc)
            try:
                mask_nodes = ('SolidMask', 'MaskComposite', 'ConditioningSetMask', 'ConditioningCombine')
                mask_available = all(bool(self._get('/object_info/' + name, 3).get(name)) for name in mask_nodes)
                mask_error = None if mask_available else '缺少区域 mask 标准节点。'
            except Exception as exc:
                mask_available, mask_error = False, '区域 mask 节点不可用：' + str(exc)
            return {'available': True, 'checkpoints': checkpoints, 'error': None if checkpoints else '当前未发现可用 checkpoint。',
                    'region_control': {'available': region_available, 'error': region_error},
                    'mask_control': {'available': mask_available, 'error': mask_error}}
        except Exception as exc:
            return {'available': False, 'checkpoints': [], 'error': 'ComfyUI 不可访问：' + str(exc),
                    'region_control': {'available': False, 'error': 'ComfyUI 不可访问。'},
                    'mask_control': {'available': False, 'error': 'ComfyUI 不可访问。'}}

    @staticmethod
    def workflow(checkpoint, prompt, negative, seed, aspect, regions=None, conditioning_mode=None):
        if conditioning_mode not in (None, MASKED_REGIONS_MODE):
            raise ValueError('区域工作流模式无效。')
        if conditioning_mode == MASKED_REGIONS_MODE and not regions:
            raise ValueError('分区工作流需要非空物体区域。')
        sizes = {'landscape': (1152, 768), 'square': (1024, 1024), 'portrait': (768, 1152)}
        width, height = sizes[aspect]
        workflow = {
            '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': checkpoint}},
            '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': prompt, 'clip': ['1', 1]}},
            '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': negative, 'clip': ['1', 1]}},
            '4': {'class_type': 'EmptyLatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
            '5': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'positive': ['2', 0],
                'negative': ['3', 0], 'latent_image': ['4', 0], 'seed': seed, 'steps': 16,
                'cfg': 7, 'sampler_name': 'euler', 'scheduler': 'normal', 'denoise': 1}},
            '6': {'class_type': 'VAEDecode', 'inputs': {'samples': ['5', 0], 'vae': ['1', 2]}},
            '7': {'class_type': 'SaveImage', 'inputs': {'images': ['6', 0], 'filename_prefix': 'article_illustrator'}}}
        if conditioning_mode == MASKED_REGIONS_MODE:
            workflow['8'] = {'class_type': 'SolidMask', 'inputs': {'value': 1.0, 'width': width, 'height': height}}
            workflow['9'] = {'class_type': 'SolidMask', 'inputs': {'value': 0.0, 'width': width, 'height': height}}
            background, conditions = ['8', 0], []
            for index, region in enumerate(regions):
                local, whole, subtract, encode, condition = (str(10 + 5 * index + offset) for offset in range(5))
                workflow[local] = {'class_type': 'SolidMask', 'inputs': {
                    'value': 1.0, 'width': region['width'], 'height': region['height']}}
                workflow[whole] = {'class_type': 'MaskComposite', 'inputs': {
                    'destination': ['9', 0], 'source': [local, 0], 'x': region['x'], 'y': region['y'], 'operation': 'add'}}
                workflow[subtract] = {'class_type': 'MaskComposite', 'inputs': {
                    'destination': background, 'source': [local, 0], 'x': region['x'], 'y': region['y'], 'operation': 'subtract'}}
                background = [subtract, 0]
                workflow[encode] = {'class_type': 'CLIPTextEncode', 'inputs': {'text': region['text'], 'clip': ['1', 1]}}
                workflow[condition] = {'class_type': 'ConditioningSetMask', 'inputs': {
                    'conditioning': [encode, 0], 'mask': [whole, 0], 'strength': region['strength'], 'set_cond_area': 'default'}}
                conditions.append([condition, 0])
            node = 10 + 5 * len(conditions)
            workflow[str(node)] = {'class_type': 'ConditioningSetMask', 'inputs': {
                'conditioning': ['2', 0], 'mask': background, 'strength': 1.0, 'set_cond_area': 'default'}}
            positive = [str(node), 0]
            for condition in conditions:
                node += 1
                workflow[str(node)] = {'class_type': 'ConditioningCombine', 'inputs': {
                    'conditioning_1': positive, 'conditioning_2': condition}}
                positive = [str(node), 0]
            workflow['5']['inputs']['positive'] = positive
            return workflow
        positive = ['2', 0]
        for index, region in enumerate(regions or []):
            encode_id, area_id, combine_id = (str(8 + index * 3 + offset) for offset in range(3))
            workflow[encode_id] = {'class_type': 'CLIPTextEncode', 'inputs': {'text': region['text'], 'clip': ['1', 1]}}
            workflow[area_id] = {'class_type': 'ConditioningSetArea', 'inputs': {
                'conditioning': [encode_id, 0], **{key: region[key] for key in ('width', 'height', 'x', 'y', 'strength')}}}
            workflow[combine_id] = {'class_type': 'ConditioningCombine', 'inputs': {
                'conditioning_1': positive, 'conditioning_2': [area_id, 0]}}
            positive = [combine_id, 0]
        workflow['5']['inputs']['positive'] = positive
        return workflow

    def submit(self, checkpoint, prompt, negative, seed, aspect, submission_key, regions=None, conditioning_mode=None):
        try:
            workflow = self.workflow(checkpoint, prompt, negative, seed, aspect, regions, conditioning_mode)
        except Exception as exc:
            raise RejectedSubmissionError('本地工作流准备失败，尚未发送生图请求：' + str(exc)) from exc
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(self.url + '/prompt', json={
                'prompt': workflow,
                'client_id': submission_key,
                'extra_data': {'illustrator_submission_key': submission_key}})
            if response.status_code == 400:
                try:
                    rejection = response.json()
                except (ValueError, TypeError):
                    rejection = None
                # server.py returns this exact validation envelope before prompt_queue.put.
                # Other statuses, ambiguous bodies, and transport failures remain unknown.
                if (isinstance(rejection, dict) and 'prompt_id' not in rejection and
                        isinstance(rejection.get('error'), dict) and rejection['error'] and
                        isinstance(rejection.get('node_errors'), dict)):
                    raise RejectedSubmissionError('本地 ComfyUI 校验拒绝，未入队：' +
                        str(rejection['error'].get('message', rejection['error'].get('type', 'invalid prompt')))[:500])
            response.raise_for_status()
            request_id = response.json().get('prompt_id')
            if not request_id:
                raise ModelError('ComfyUI 提交未返回 request_id，需检查历史后再处理。')
            return request_id

    def find_submission(self, submission_key):
        """Only reads shared queue/history; never deletes or interrupts other jobs."""
        queue = self._get('/queue')
        history = self._get('/history?max_items=200')
        entries = list(queue.get('queue_running', [])) + list(queue.get('queue_pending', []))
        entries += [record.get('prompt', []) for record in history.values()]
        for entry in entries:
            if len(entry) >= 4:
                extra = entry[3] or {}
                if extra.get('illustrator_submission_key') == submission_key or extra.get('client_id') == submission_key:
                    return entry[1]
        return None

    def result(self, request_id, destination: Path):
        start = time.monotonic()
        while time.monotonic() - start < self.result_timeout:
            data = self._get('/history/' + request_id)
            record = data.get(request_id)
            if record:
                status = record.get('status', {})
                if status.get('status_str') == 'error':
                    raise CompletedRequestError('ComfyUI 已记录生成错误；原 request_id 保留，未重复提交。')
                images = [image for output in record.get('outputs', {}).values() for image in output.get('images', [])]
                if images:
                    item = images[0]
                    with httpx.Client(timeout=60, trust_env=False) as client:
                        response = client.get(self.url + '/view', params={k: item.get(k, '') for k in ('filename', 'subfolder', 'type')})
                        response.raise_for_status()
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = destination.with_suffix('.part')
                        temporary.write_bytes(response.content)
                        temporary.replace(destination)
                    return destination
                if status.get('completed'):
                    raise CompletedRequestError('ComfyUI 已完成请求但没有可读取图片。')
            time.sleep(self.poll_seconds)
        raise ModelError('等待 ComfyUI 结果超时；原 request_id 保留，下次恢复只检查此请求。')
