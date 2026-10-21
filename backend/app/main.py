"""Loopback-only article illustration API, durable authorization and model queue."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from .director import MASKED_REGIONS_MODE, compile_controls, generation_text, region_areas, validate_director
from .cloud import CloudConfigStore, CloudImageAdapter, compose_cloud_prompts
from .models import ComfyAdapter, CompletedRequestError, DEFAULT_STYLE, ModelError, OllamaAdapter, RejectedSubmissionError, TEXT_MODEL
from .privacy import generated_image_hashes, private_export_copy, private_image_copy, public_error, public_task, sanitize_new_image, UnsafeLegacyExport
from .store import Store
from .paths import default_data_directory

ROOT = Path(__file__).resolve().parents[2]
GENRES = ['academic', 'news', 'novel', 'report']


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def public(task):
    return public_task(task)


def bad(message, status=400):
    raise HTTPException(status, message)


def busy(task):
    return task.get('_prompting') or any(j['status'] in ('queued', 'running', 'awaiting_agent') for j in task['jobs'])


def unresolved(task):
    return any(s.get('state') in ('submitting', 'submitted', 'awaiting_agent') for j in task['jobs'] for s in j.get('submissions', []))


def invalidate(task, clear_prompts=False):
    task['approval'] = None
    task.pop('prompt_failure_evidence', None)
    task['selections'] = {}
    if clear_prompts:
        task['prompts'] = {}
        task['edited_prompt_ids'] = []
    for asset in task['assets']:
        asset['stale'] = True


def prompt_hash(task):
    return digest({k: {'text': v['text'], 'negative': v.get('negative', ''),
                      **({'regions': v['regions']} if 'regions' in v else {}),
                      **({'background_text': v['background_text']} if 'background_text' in v else {}),
                      **({'global_text': v['global_text']} if 'global_text' in v else {}),
                      **({'conditioning_mode': v['conditioning_mode']} if 'conditioning_mode' in v else {})}
                   for k, v in task['prompts'].items()})


def snapshot(task):
    approval = task.get('approval')
    if not task.get('plan') or not approval:
        bad('请先确认方案并授权当前版本。', 409)
    result = {'document_hash': task['document']['source_hash'], 'plan_version': task['plan']['version'],
              'plan_hash': digest(task['plan']), 'prompt_hash': prompt_hash(task),
              'checkpoint': approval['checkpoint'], 'approval_id': approval['id']}
    for field in ('image_target', 'image_targets'):
        if field in approval:
            result[field] = copy.deepcopy(approval[field])
    for key in ('document_hash', 'plan_version', 'plan_hash', 'prompt_hash', 'checkpoint'):
        if approval.get(key) != result[key]:
            bad('授权已失效，请重新确认当前方案和提示词。', 409)
    return result


def matches(task, snap):
    try:
        return snapshot(task) == snap
    except HTTPException:
        return False


def current_asset(task, asset):
    try:
        snap = snapshot(task)
    except HTTPException:
        return False
    return not asset.get('stale') and all(asset.get(k) == v for k, v in snap.items())


def validate_prompts(prompts, slots, english=False, require_background=False):
    if not isinstance(prompts, dict) or set(prompts) != {s['id'] for s in slots}:
        bad('提示词必须覆盖当前全部图位，不能包含未知图位。')
    clean = {}
    slot_map = {slot['id']: slot for slot in slots}
    for key, value in prompts.items():
        if not isinstance(value, dict):
            bad('提示词格式必须为 text 与 negative。')
        text = str(value.get('text', '')).strip()
        negative = str(value.get('negative', '')).strip()
        if not text or len(text) > 6000 or len(negative) > 3000:
            bad('提示词为空或超过长度限制。')
        if english and (re.search('[\u4e00-\u9fff]', text + negative) or not re.search('[a-zA-Z]', text)):
            bad('正式生图提示词转换未得到英文，请修改或重新转换。', 422)
        clean[key] = {'text': text, 'negative': negative}
        objects = slot_map[key].get('director', {}).get('objects', [])
        if objects and (require_background or 'background_text' in value):
            background = value.get('background_text')
            if not isinstance(background, str) or not background.strip() or len(background) > 3000:
                bad('区域方案需要非空、最多 3000 字符的背景提示词，请重新转换。', 422)
            if english and (re.search('[\u4e00-\u9fff]', background) or not re.search('[a-zA-Z]', background)):
                bad('背景提示词转换未得到英文，请重新转换。', 422)
            clean[key]['background_text'] = background.strip()
        elif 'background_text' in value:
            bad('无物体区域的方案不能包含独立背景提示词。')
        if 'global_text' in value:
            global_text = value['global_text']
            if not objects:
                bad('无物体区域的方案不能包含独立全局提示词。')
            if not isinstance(global_text, str) or not global_text.strip() or len(global_text) > 6000:
                bad('全局提示词须为非空、最多 6000 字符的文本。', 422)
            if english and (re.search('[\u4e00-\u9fff]', global_text) or not re.search('[a-zA-Z]', global_text)):
                bad('全局提示词转换未得到英文，请重新转换。', 422)
            clean[key]['global_text'] = global_text.strip()
        if 'conditioning_mode' in value:
            mode = value['conditioning_mode']
            if mode != MASKED_REGIONS_MODE or not isinstance(mode, str):
                bad('分区提示词模式无效。', 422)
            if not objects or not clean[key].get('background_text') or not clean[key].get('global_text'):
                bad('分区提示词模式需要物体区域和冻结背景。', 422)
            if clean[key]['global_text'] != clean[key]['background_text']:
                bad('分区模式的全局提示词必须与冻结背景一致。', 422)
            clean[key]['conditioning_mode'] = mode
        if objects or 'regions' in value:
            regions = value.get('regions')
            if not isinstance(regions, list) or len(regions) != len(objects):
                bad('局部提示词必须恰好覆盖导演台的全部物体。')
            region_map = {}
            for region in regions:
                if not isinstance(region, dict) or set(region) != {'object_id', 'text'}:
                    bad('局部提示词格式必须为 object_id 与 text。')
                object_id, region_text = region['object_id'], region['text']
                if not isinstance(object_id, str) or object_id in region_map:
                    bad('局部提示词的物体 ID 无效或重复。')
                if not isinstance(region_text, str) or not region_text.strip() or len(region_text) > 1200:
                    bad('每个局部提示词须为非空、最多 1200 字符的文本。')
                if english and (re.search('[\u4e00-\u9fff]', region_text) or not re.search('[a-zA-Z]', region_text)):
                    bad('局部提示词转换未得到英文，请修改或重新转换。', 422)
                region_map[object_id] = region_text.strip()
            if set(region_map) != {item['id'] for item in objects}:
                bad('局部提示词遗漏或包含未知物体 ID。')
            clean[key]['regions'] = [{'object_id': item['id'], 'text': region_map[item['id']]} for item in objects]
    return clean


def validate_slots(slots, document):
    if not isinstance(slots, list) or len(slots) > 5:
        bad('最多支持 5 个图位。')
    paragraphs = {p['id']: p for p in document['paragraphs']}
    ids = set()
    clean = []
    for slot in slots:
        if not isinstance(slot, dict):
            bad('图位格式不正确。')
        slot_id = str(slot.get('id') or uuid.uuid4())
        if len(slot_id) > 100 or slot_id in ids:
            bad('图位 ID 重复或无效。')
        ids.add(slot_id)
        paragraph_id = slot.get('paragraph_id')
        if paragraph_id not in paragraphs or paragraphs[paragraph_id]['kind'] != 'paragraph' or not paragraphs[paragraph_id].get('insertable', True):
            bad('图位必须引用原稿正文段落，不能引用标题、表格或未知段落。')
        aspect = slot.get('aspect_ratio', 'landscape')
        if aspect not in ('landscape', 'square', 'portrait'):
            bad('图像比例无效。')
        item = {'id': slot_id, 'paragraph_id': paragraph_id, 'aspect_ratio': aspect}
        for field in ('reason', 'description', 'caption', 'risk'):
            item[field] = str(slot.get(field, '')).strip()
            if len(item[field]) > 4000:
                bad('图位描述超过长度限制。')
        if not item['description'] or not item['caption']:
            bad('每个图位需要视觉描述和图注。')
        if 'director' in slot:
            try:
                item['director'] = validate_director(slot['director'])
            except ValueError as exc:
                bad(str(exc))
        clean.append(item)
    return clean


class Service:
    def __init__(self, directory, ollama=None, comfy=None):
        self.store = Store(Path(directory))
        self.ollama = ollama or OllamaAdapter()
        self.comfy = comfy or ComfyAdapter()
        self.cloud_config = CloudConfigStore(Path(directory))
        self.cloud = CloudImageAdapter(self.cloud_config, Path(directory) / 'cloud-requests')
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-model')
        self.model_lock = threading.RLock()
        self.scheduled = set()
        self.schedule_lock = threading.Lock()
        self.asset_locks = {}
        self.asset_locks_guard = threading.Lock()

    def asset_lock(self, task_id, asset_id):
        with self.asset_locks_guard:
            return self.asset_locks.setdefault((task_id, asset_id), threading.RLock())

    def processed_path(self, task_id, asset, version=None):
        directory = self.directory(task_id) / 'assets'
        versions = asset.get('processing_versions', {})
        if version:
            record = versions.get(version)
            if not record:
                bad('处理图片版本不存在。', 404)
            return directory / record['filename']
        current = (asset.get('processing') or {}).get('version')
        if current and current in versions:
            return directory / versions[current]['filename']
        return directory / (asset['id'] + '-processed.png')

    def require(self, task_id):
        task = self.store.get(task_id)
        if task is None:
            bad('任务不存在。', 404)
        return task

    def directory(self, task_id):
        path = self.store.directory / 'tasks' / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def update(self, task_id, fn):
        self.require(task_id)
        def change(task):
            result = fn(task)
            task['updated_at'] = now()
            return result
        return self.store.mutate(task_id, change)

    def new_task(self, document, genre, style, title, source_path=None):
        if genre not in GENRES:
            bad('文章类型无效。')
        if document.get('character_count', 0) > 10000:
            bad('DEMO 最多支持 10,000 个正文字符。', 413)
        if not document.get('paragraphs'):
            bad('文档没有可分析的正文。')
        task = {'id': str(uuid.uuid4()), 'title': title or document.get('title') or '未命名文章',
                'genre': genre, 'style': style or DEFAULT_STYLE, 'status': 'imported', 'document': document,
                'plan': None, 'approval': None, 'prompts': {}, 'assets': [], 'selections': {},
                'jobs': [], 'exports': [], 'error': None, 'created_at': now(), 'updated_at': now(),
                'source_path': str(source_path) if source_path else None, 'edited_prompt_ids': [],
                'cancel_requested': False, '_prompting': False}
        self.store.put(task)
        return task

    def schedule(self, task_id, job_id):
        with self.schedule_lock:
            if job_id in self.scheduled:
                return
            self.scheduled.add(job_id)
        self.executor.submit(self.run_job, task_id, job_id)

    def recover(self):
        for task in self.store.list():
            if task.get('_prompting'):
                def reset(t):
                    t['_prompting'] = False
                    t['approval'] = None
                    t['status'] = 'plan_ready'
                    t['error'] = '批准时应用中断，未建立有效授权，请重新确认。'
                self.update(task['id'], reset)
            for job in task['jobs']:
                if job['status'] in ('queued', 'running'):
                    self.schedule(task['id'], job['id'])

    def run_job(self, task_id, job_id):
        try:
            with self.model_lock:
                task = self.require(task_id)
                job = next(j for j in task['jobs'] if j['id'] == job_id)
                if job['status'] not in ('queued', 'running'):
                    return
                if task.get('cancel_requested') and not job.get('submissions'):
                    self.finish(task_id, job_id, 'cancelled', '已取消未提交的任务。')
                    return
                def running(t):
                    j = next(j for j in t['jobs'] if j['id'] == job_id)
                    j['status'] = 'running'
                    j['message'] = '本地模型执行中。'
                self.update(task_id, running)
                if job['kind'] == 'analysis':
                    self.analyze(task_id, job_id)
                else:
                    self.generate(task_id, job_id)
        except Exception as exc:
            self.finish(task_id, job_id, 'failed', str(exc) if not isinstance(exc, HTTPException) else str(exc.detail))
        finally:
            with self.schedule_lock:
                self.scheduled.discard(job_id)

    def finish(self, task_id, job_id, status, message):
        def change(t):
            j = next(j for j in t['jobs'] if j['id'] == job_id)
            j['status'], j['message'], j['error'] = status, message, message if status == 'failed' else None
            if status == 'completed':
                j['progress'] = 1
            if j.get('snapshot') and not matches(t, j['snapshot']):
                return
            t['status'] = ('plan_ready' if j['kind'] == 'analysis' else 'review') if status == 'completed' else status
            t['error'] = j['error']
        self.update(task_id, change)

    def analyze(self, task_id, job_id):
        task = self.require(task_id)
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        result = self.ollama.analyze(task['document'], task['genre'], task['style'], job['model'])
        coverage = result.get('coverage')
        expected = [p['id'] for p in task['document']['paragraphs']]
        if not isinstance(coverage, list) or len(coverage) != len(expected) or set(coverage) != set(expected):
            raise ModelError('全文覆盖校验失败：模型遗漏、重复或创造了段落 ID。未保存不完整方案。')
        slots = validate_slots(result.get('slots'), task['document'])
        def change(t):
            if t.get('cancel_requested'):
                return False
            if t['document']['source_hash'] != job['document_hash']:
                raise ModelError('原稿版本发生变化，拒绝保存旧分析结果。')
            version = (t['plan']['version'] if t.get('plan') else 0) + 1
            invalidate(t, True)
            t['plan'] = {'version': version, 'summary': str(result.get('summary', '')),
                         'coverage': coverage, 'style': t['style'], 'slots': slots, 'model': job['model'], 'source': 'ollama',
                         'read_chunks': result.get('read_chunks', []), 'coverage_evidence': result.get('coverage_evidence', {})}
            return True
        _, saved = self.update(task_id, change)
        self.finish(task_id, job_id, 'completed' if saved else 'cancelled', '全文建议已保存。' if saved else '分析已取消，结果未覆盖方案。')

    def generate(self, task_id, job_id):
        task = self.require(task_id)
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        snap = job['snapshot']
        if snap.get('image_target'):
            return self.generate_cloud(task_id, job_id)
        if not matches(task, snap):
            raise ModelError('生成授权已失效，拒绝执行旧作业。')
        slots = job['slots']
        for index, slot in enumerate(slots):
            task = self.require(task_id)
            job = next(j for j in task['jobs'] if j['id'] == job_id)
            if not matches(task, snap):
                raise ModelError('生成版本不再匹配，已停止后续提交。')
            if any(a['slot_id'] == slot['id'] and current_asset(task, a) for a in task['assets']):
                continue
            submission = next((s for s in job.get('submissions', []) if s['slot_id'] == slot['id']), None)
            if not submission:
                if task.get('cancel_requested'):
                    break
                if task['approval']['attempts_used'] >= task['approval']['max_attempts']:
                    raise ModelError('当前授权尝试次数已用尽，请重新确认后再执行。')
                areas = region_areas(slot, job['prompts'][slot['id']])
                mode = job['prompts'][slot['id']].get('conditioning_mode')
                if mode not in (None, MASKED_REGIONS_MODE):
                    raise ModelError('区域提示词模式无效；没有提交生图请求。')
                control_key = 'mask_control' if mode == MASKED_REGIONS_MODE else 'region_control'
                if areas and not self.comfy.health().get(control_key, {}).get('available'):
                    raise ModelError('本地 ComfyUI 区域控制节点不可用；没有提交生图请求或降级为普通文生图。')
                submission = {'slot_id': slot['id'], 'key': job_id + ':' + slot['id'], 'request_id': None,
                              'state': 'submitting', 'seed': secrets.randbelow(2**31), 'created_at': now()}
                def intent(t):
                    if not matches(t, snap) or t.get('cancel_requested'):
                        raise ModelError('提交前授权或取消状态发生变化。')
                    j = next(j for j in t['jobs'] if j['id'] == job_id)
                    j.setdefault('submissions', []).append(submission)
                    j['attempts'] += 1
                    t['approval']['attempts_used'] += 1
                    j['message'] = '已记录提交意图，准备请求本地 ComfyUI。'
                self.update(task_id, intent)
                try:
                    extra = {'regions': areas} if areas else {}
                    prepared_prompt = job['prompts'][slot['id']]
                    if 'conditioning_mode' in prepared_prompt:
                        extra['conditioning_mode'] = prepared_prompt['conditioning_mode']
                    effective_text = generation_text(prepared_prompt, regional=bool(areas))
                    request_id = self.comfy.submit(snap['checkpoint'], effective_text,
                        job['prompts'][slot['id']]['negative'], submission['seed'], slot['aspect_ratio'], submission['key'], **extra)
                except RejectedSubmissionError as exc:
                    def rejected(t):
                        j = next(j for j in t['jobs'] if j['id'] == job_id)
                        s = next(s for s in j['submissions'] if s['slot_id'] == slot['id'])
                        s['state'], s['rejection'] = 'failed', str(exc)
                        j['message'] = '模型明确未接受请求，允许修改方案；没有降级或自动重提。'
                    self.update(task_id, rejected)
                    raise
                except Exception:
                    request_id = self.comfy.find_submission(submission['key'])
                    if not request_id:
                        raise ModelError('生图提交结果未知；已查本地队列与历史，未定位请求。为避免重复生图没有重提，请人工核查。')
            else:
                request_id = submission.get('request_id')
                if not request_id:
                    request_id = self.comfy.find_submission(submission['key'])
                    if not request_id:
                        raise ModelError('恢复时提交结果未知；只查队列与历史，没有重复提交。请人工核查原请求。')
            def recorded(t):
                j = next(j for j in t['jobs'] if j['id'] == job_id)
                s = next(s for s in j['submissions'] if s['slot_id'] == slot['id'])
                s['request_id'], s['state'] = request_id, 'submitted'
                if request_id not in j['request_ids']:
                    j['request_ids'].append(request_id)
                j['message'] = '已提交；等待此 request_id 的历史图片。取消将停止后续提交并保留当前结果。'
            self.update(task_id, recorded)
            asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, task_id + ':' + request_id + ':' + slot['id']))
            destination = self.directory(task_id) / 'assets' / (asset_id + '.png')
            try:
                self.comfy.result(request_id, destination)
            except CompletedRequestError:
                def terminal(t):
                    j = next(j for j in t['jobs'] if j['id'] == job_id)
                    next(s for s in j['submissions'] if s['slot_id'] == slot['id'])['state'] = 'failed'
                self.update(task_id, terminal)
                raise
            sanitize_new_image(destination)
            with Image.open(destination) as image:
                image.verify()
            with Image.open(destination) as image:
                width, height = image.size
            prepared_prompt = job['prompts'][slot['id']]
            areas = region_areas(slot, prepared_prompt)
            effective_text = generation_text(prepared_prompt, regional=bool(areas))
            asset = {'id': asset_id, 'slot_id': slot['id'], 'prompt': effective_text,
                     'negative_prompt': job['prompts'][slot['id']]['negative'], 'source': 'comfyui',
                     'model': snap['checkpoint'], 'request_id': request_id, 'seed': submission['seed'],
                     'width': width, 'height': height, 'url': f'/api/assets/{task_id}/{asset_id}/original',
                     'processed_url': None, 'processed_width': None, 'processed_height': None,
                     'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(), 'processing': None,
                     'created_at': now(), **snap, 'stale': False}
            if slot.get('director'):
                asset['director'] = copy.deepcopy(slot['director'])
                asset['regions'] = copy.deepcopy(job['prompts'][slot['id']].get('regions', []))
                asset['region_areas'] = region_areas(slot, job['prompts'][slot['id']])
                if 'background_text' in prepared_prompt:
                    asset['scene_prompt'] = prepared_prompt['text']
                    asset['background_text'] = prepared_prompt['background_text']
                if 'global_text' in prepared_prompt:
                    asset['scene_prompt'] = prepared_prompt['text']
                    asset['global_text'] = prepared_prompt['global_text']
                if 'conditioning_mode' in prepared_prompt:
                    asset['conditioning_mode'] = prepared_prompt['conditioning_mode']
            def saved(t):
                asset['stale'] = not matches(t, snap)
                if not any(a['id'] == asset_id for a in t['assets']):
                    t['assets'].append(asset)
                j = next(j for j in t['jobs'] if j['id'] == job_id)
                next(s for s in j['submissions'] if s['slot_id'] == slot['id'])['state'] = 'saved'
                j['progress'] = (index + 1) / max(1, len(slots))
            self.update(task_id, saved)
        task = self.require(task_id)
        self.finish(task_id, job_id, 'cancelled' if task.get('cancel_requested') else 'completed',
                    '已停止后续提交；已完成请求的图片已保留。' if task.get('cancel_requested') else '当前授权的候选图片已生成。')

    def save_cloud_result(self, task_id, job_id, slot, submission, destination):
        task = self.require(task_id)
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        snap = job['snapshot']
        prepared = job['prompts'][slot['id']]
        target = snap['image_targets'][slot['id']]
        sanitize_new_image(destination)
        with Image.open(destination) as image:
            image.verify()
        with Image.open(destination) as image:
            width, height = image.size
        asset_id = destination.stem
        asset = {'id': asset_id, 'slot_id': slot['id'], 'prompt': prepared['text'],
                 'negative_prompt': prepared.get('negative', ''), 'source': target['provider'],
                 'model': target['model'], 'request_id': submission['request_id'], 'seed': None,
                 'width': width, 'height': height, 'url': f'/api/assets/{task_id}/{asset_id}/original',
                 'processed_url': None, 'processed_width': None, 'processed_height': None,
                 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(), 'processing': None,
                 'created_at': now(), **snap, 'stale': False}
        if slot.get('director'):
            asset['director'] = copy.deepcopy(slot['director'])
            asset['regions'] = copy.deepcopy(prepared.get('regions', []))
        def saved(t):
            if not matches(t, snap):
                raise ModelError('返回图片与当前授权不匹配，未插入资产。')
            j = next(j for j in t['jobs'] if j['id'] == job_id)
            item = next(s for s in j['submissions'] if s['slot_id'] == slot['id'])
            if not any(a['id'] == asset_id for a in t['assets']):
                t['assets'].append(asset)
            item['state'] = 'saved'
            j['progress'] = sum(s['state'] == 'saved' for s in j['submissions']) / max(1, len(j['slots']))
        self.update(task_id, saved)

    def generate_cloud(self, task_id, job_id):
        task = self.require(task_id)
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        snap = job['snapshot']
        if not matches(task, snap):
            raise ModelError('云端生图授权已失效，未发送请求。')
        agent_mode = snap['image_target']['provider'] == 'codex'
        for slot in job['slots']:
            task = self.require(task_id)
            job = next(j for j in task['jobs'] if j['id'] == job_id)
            if not matches(task, snap):
                raise ModelError('云端生图授权已失效，未发送请求。')
            if any(a['slot_id'] == slot['id'] and current_asset(task, a) for a in task['assets']):
                continue
            submission = next((s for s in job['submissions'] if s['slot_id'] == slot['id']), None)
            target = snap['image_targets'][slot['id']]
            if not submission:
                if task.get('cancel_requested'):
                    break
                self.cloud_config.ensure_target(target)
                submission = {'slot_id': slot['id'], 'key': job_id + ':' + slot['id'],
                              'request_id': 'codex-' + uuid.uuid4().hex if agent_mode else None,
                              'state': 'awaiting_agent' if agent_mode else 'submitting', 'seed': None,
                              'created_at': now()}
                def intent(t):
                    if not matches(t, snap) or t.get('cancel_requested'):
                        raise ModelError('提交前授权或取消状态发生变化。')
                    if t['approval']['attempts_used'] >= t['approval']['max_attempts']:
                        raise ModelError('当前授权尝试次数已用尽，请重新确认。')
                    j = next(j for j in t['jobs'] if j['id'] == job_id)
                    j['submissions'].append(submission)
                    j['attempts'] += 1
                    t['approval']['attempts_used'] += 1
                    j['message'] = '已安排 GPT 内置协作，等待当前 Codex 会话执行。' if agent_mode else '正在向所选云端模型发送已确认画面。'
                    if agent_mode:
                        j['request_ids'].append(submission['request_id'])
                self.update(task_id, intent)
                if agent_mode:
                    continue
                try:
                    request_id = self.cloud.submit(target, job['prompts'][slot['id']]['text'], submission['key'])
                except RejectedSubmissionError:
                    def rejected(t):
                        j = next(j for j in t['jobs'] if j['id'] == job_id)
                        next(s for s in j['submissions'] if s['slot_id'] == slot['id'])['state'] = 'failed'
                    self.update(task_id, rejected)
                    raise
                except Exception:
                    request_id = self.cloud.find_submission(submission['key'])
                    if not request_id:
                        raise ModelError('云端提交结果未知；未自动重提，请保留原作业核查。')
            else:
                if agent_mode:
                    continue
                request_id = submission.get('request_id') or self.cloud.find_submission(submission['key'])
                if not request_id:
                    raise ModelError('云端提交结果未知；未自动重提，请保留原作业核查。')
            def recorded(t):
                j = next(j for j in t['jobs'] if j['id'] == job_id)
                s = next(s for s in j['submissions'] if s['slot_id'] == slot['id'])
                s['request_id'], s['state'] = request_id, 'submitted'
                if request_id not in j['request_ids']:
                    j['request_ids'].append(request_id)
                j['message'] = '云端模型已返回，正在保存原始图片。'
            self.update(task_id, recorded)
            submission = {**submission, 'request_id': request_id}
            asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, task_id + ':' + request_id + ':' + slot['id']))
            destination = self.directory(task_id) / 'assets' / (asset_id + '.png')
            try:
                self.cloud.result(request_id, destination)
            except CompletedRequestError:
                def terminal(t):
                    j = next(j for j in t['jobs'] if j['id'] == job_id)
                    next(s for s in j['submissions'] if s['slot_id'] == slot['id'])['state'] = 'failed'
                self.update(task_id, terminal)
                raise
            self.save_cloud_result(task_id, job_id, slot, submission, destination)
        task = self.require(task_id)
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        if agent_mode and any(s['state'] in ('awaiting_agent', 'submitted') for s in job['submissions']):
            def waiting(t):
                j = next(j for j in t['jobs'] if j['id'] == job_id)
                j['status'] = 'awaiting_agent'
                j['message'] = '等待当前 Codex 会话使用内置生图并回填图片。'
                t['status'] = 'generating'
            self.update(task_id, waiting)
        else:
            self.finish(task_id, job_id, 'cancelled' if task.get('cancel_requested') else 'completed',
                        '已停止后续请求，返回图片已保留。' if task.get('cancel_requested') else '云端候选图片已保存。')


def create_app(data_dir=None, ollama_adapter=None, comfy_adapter=None, recover=True):
    directory = Path(data_dir if data_dir is not None else default_data_directory()).expanduser()
    service = Service(directory, ollama_adapter, comfy_adapter)

    @asynccontextmanager
    async def lifespan(app):
        if recover:
            service.recover()
        yield
        service.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title='图序社区版 · Tuxu Community', version='0.1.0', lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(RequestValidationError)
    async def private_validation_error(request, exc):
        # FastAPI's default 422 body echoes invalid inputs, including private prompt bodies.
        return JSONResponse({'detail': '请求格式不正确，请检查输入。'}, status_code=422)

    @app.exception_handler(HTTPException)
    async def private_http_error(request, exc):
        messages = {403: '本地访问校验未通过，请刷新页面。', 404: '所请求的内容不存在。',
                    409: '操作条件已改变，请刷新并重新保存或确认。',
                    410: '此操作已停用，请通过画面描述和导演设置修改方案。',
                    413: '文件或正文超过本地演示的大小限制。'}
        message = '旧文件需要重新导出，以保留原稿图片并保护生成信息。' if exc.detail == '旧文件需要重新导出。' else messages.get(exc.status_code)
        if not message:
            message = public_error(exc.detail) if exc.status_code == 422 else '配置或输入不正确，请调整后重试。'
        return JSONResponse({'detail': message}, status_code=exc.status_code)

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        host = urlparse('http://' + request.headers.get('host', '')).hostname
        if host not in ('127.0.0.1', 'localhost', '::1'):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': '仅允许本机访问。'}, 403)
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': '拒绝非同源请求。'}, 403)
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not secrets.compare_digest(
                request.headers.get('x-local-token', ''), service.store.token):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': '本地写请求令牌无效，请刷新页面。'}, 403)
        return await call_next(request)

    @app.get('/api/bootstrap')
    def bootstrap():
        text = service.ollama.health()
        image = service.comfy.health()
        checkpoints = image.get('checkpoints', [])
        cloud = service.cloud_config.list_public()
        preferred = next((p['id'] for p in cloud['profiles'] if p.get('ready') and
                          p.get('verification', {}).get('generation') == 'succeeded' and
                          p.get('verification', {}).get('connection') != 'failed'), 'local')
        return {'application': {'id': 'tuxu-community', 'version': '0.1.0'},
                'token': service.store.token, 'genres': GENRES, 'tasks': [public(t) for t in service.store.list()],
                'models': {'ollama': text, 'comfy': image, 'cloud': cloud}, 'defaults': {'text_model': text.get('model', TEXT_MODEL),
                'image_provider': preferred,
                'image_checkpoint': checkpoints[0] if checkpoints else '', 'style': DEFAULT_STYLE}}

    @app.get('/api/image-providers')
    def image_providers():
        return service.cloud_config.list_public()

    @app.put('/api/image-providers/{provider_id}')
    def configure_image_provider(provider_id: str, body: dict):
        for t in service.store.list():
            target = (t.get('approval') or {}).get('image_target') or {}
            if target.get('id') == provider_id and (busy(t) or unresolved(t)):
                bad('此服务仍有未决请求，请先核查原作业。', 409)
        try:
            return service.cloud_config.update(provider_id, body)
        except (ValueError, ModelError):
            bad('云端模型配置不正确，请检查地址、模型和密钥。')

    @app.post('/api/image-providers/{provider_id}/test')
    def test_image_provider(provider_id: str, body: dict):
        try:
            return service.cloud_config.test(provider_id)
        except (ValueError, ModelError):
            return {'ok': False, 'model_found': False, 'error': '服务连接未通过，请核查配置。'}

    @app.get('/api/tasks')
    def tasks():
        return [public(t) for t in service.store.list()]

    @app.get('/api/tasks/{task_id}')
    def get_task(task_id: str):
        return public(service.require(task_id))

    @app.post('/api/tasks/paste')
    def paste(body: dict):
        from .documents import parse_text
        text = body.get('text', '')
        if not isinstance(text, str) or not text.strip():
            bad('请提供文章正文。')
        if len(text) > 10000:
            bad('DEMO 最多支持 10,000 个正文字符。', 413)
        try:
            document = parse_text(text, str(body.get('title', '未命名文章')))
        except ValueError as exc:
            bad(str(exc))
        return public(service.new_task(document, body.get('genre', 'report'), str(body.get('style', DEFAULT_STYLE)), body.get('title')))

    @app.post('/api/tasks/import')
    async def import_file(file: UploadFile = File(...), genre: str = Form('report'), style: str = Form(DEFAULT_STYLE), title: str = Form('')):
        from .documents import parse_document
        extension = Path(file.filename or '').suffix.lower()
        if extension not in ('.docx', '.md', '.txt'):
            bad('DEMO 仅支持 DOCX、Markdown 与 TXT。')
        data = await file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            bad('文件超过 10 MB 限额。', 413)
        temp = service.store.directory / 'imports' / (str(uuid.uuid4()) + extension)
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(data)
        try:
            document = parse_document(temp)
            document['source_filename'] = Path(file.filename or '').name
            task = service.new_task(document, genre, style, title, temp)
            target = service.directory(task['id']) / ('source' + extension)
            temp.replace(target)
            task, _ = service.update(task['id'], lambda t: t.update(source_path=str(target)))
            return public(task)
        except (ValueError, RuntimeError) as exc:
            temp.unlink(missing_ok=True)
            bad(str(exc))
        except HTTPException:
            temp.unlink(missing_ok=True)
            raise

    def new_job(task, kind, **extra):
        return {'id': str(uuid.uuid4()), 'kind': kind, 'status': 'queued', 'progress': 0,
                'message': '等待本地串行模型队列。', 'error': None, 'request_ids': [], 'attempts': 0,
                'fingerprint': None, 'plan_version': task['plan']['version'] if task.get('plan') else None,
                'approval_id': None, 'created_at': now(), **extra}

    @app.post('/api/tasks/{task_id}/analyze')
    def analyze(task_id: str, body: dict):
        model = str(body.get('model') or TEXT_MODEL)
        if len(model) > 150:
            bad('模型名称无效。')
        def change(t):
            if busy(t) or unresolved(t):
                bad('当前任务正在执行，请等待或取消完成后再分析。', 409)
            invalidate(t, True)
            t['cancel_requested'] = False
            t['error'] = None
            t['status'] = 'analyzing'
            job = new_job(t, 'analysis', model=model, document_hash=t['document']['source_hash'])
            t['jobs'].append(job)
            return job['id']
        task, job_id = service.update(task_id, change)
        service.schedule(task_id, job_id)
        return {'job_id': job_id, 'task': public(task)}

    @app.put('/api/tasks/{task_id}/plan')
    def plan(task_id: str, body: dict):
        def change(t):
            if busy(t) or unresolved(t):
                bad('执行中的任务不能修改方案，请等待或取消完成。', 409)
            if not t.get('plan') or body.get('version') != t['plan']['version']:
                bad('方案版本冲突，请刷新后修改。', 409)
            slots = validate_slots(body.get('slots'), t['document'])
            style = str(body.get('style', t['plan']['style'])).strip()
            if not style or len(style) > 3000:
                bad('统一风格为空或超过长度限制。')
            t['plan'] = {**t['plan'], 'version': t['plan']['version'] + 1, 'style': style, 'slots': slots}
            t['style'] = style
            invalidate(t, True)
            t['status'] = 'plan_ready'
            t['error'] = None
        task, _ = service.update(task_id, change)
        return public(task)

    @app.post('/api/tasks/{task_id}/approve')
    def approve(task_id: str, body: dict):
        provider = body.get('image_provider', 'local')
        if provider not in ('local', 'ark', 'openai', 'codex'):
            bad('生图服务选项无效。')
        quality = body.get('cloud_quality', 'high')
        if quality not in ('medium', 'high'):
            bad('云端图片质量选项无效。')
        checkpoint = str(body.get('checkpoint', '')).strip()
        attempts = body.get('max_attempts')
        if body.get('confirm') is not True or isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
            bad('请明确确认授权，并设置 1—10 次生图尝试上限。')
        health = {}
        if provider == 'local':
            if not checkpoint or len(checkpoint) > 300:
                bad('请选择本地 checkpoint。')
            health = service.comfy.health()
            if not health.get('available') or checkpoint not in health.get('checkpoints', []):
                bad('所选 checkpoint 当前不可用，请检查本地 ComfyUI。', 409)
        def reserve(t):
            if busy(t) or unresolved(t):
                bad('任务正在执行，请稍后确认。', 409)
            if not t.get('plan') or body.get('version') != t['plan']['version']:
                bad('方案版本冲突，请刷新后重新确认。', 409)
            if attempts < len(t['plan']['slots']):
                bad('授权尝试上限不能少于当前图位数。')
            if provider == 'local' and any(slot.get('director', {}).get('objects') for slot in t['plan']['slots']) and not health.get('mask_control', {}).get('available'):
                bad('本地 ComfyUI 缺少区域控制节点，不能批准带物体区域的方案。请恢复节点或明确删除区域后保存。', 409)
            previous_cloud = bool((t.get('approval') or {}).get('image_target'))
            invalidate(t)
            t['_prompting'] = True
            t['cancel_requested'] = False
            return {**copy.deepcopy(t), '_previous_cloud': previous_cloud}
        _, approved = service.update(task_id, reserve)
        try:
            image_targets = {}
            with service.model_lock:
                if provider != 'local':
                    if not approved['plan']['slots']:
                        bad('零图方案可以直接导出，无需云端生成授权。')
                    image_targets = {s['id']: service.cloud_config.target(provider, s['aspect_ratio'], quality)
                                     for s in approved['plan']['slots']}
                    checkpoint = next(iter(image_targets.values()))['model']
                    prompts = validate_prompts(compose_cloud_prompts(approved['plan']['slots'], approved['plan']['style']),
                                               approved['plan']['slots'])
                else:
                    prompts = approved['prompts']
                    if (approved['_previous_cloud'] or set(prompts) != {s['id'] for s in approved['plan']['slots']} or any(
                            slot.get('director', {}).get('objects') for slot in approved['plan']['slots'])):
                        prompts = service.ollama.prompts(approved['plan']['slots'], approved['plan']['style'], approved['plan']['model'])
                    prompts = validate_prompts(prompts, approved['plan']['slots'], english=True, require_background=True)
                    prompts = validate_prompts(compile_controls(prompts, approved['plan']['slots'], conditioning_mode=MASKED_REGIONS_MODE),
                                               approved['plan']['slots'], english=True, require_background=True)
            def save(t):
                if t['plan'] != approved['plan'] or t.get('cancel_requested'):
                    bad('批准流程被取消或版本改变，未保存授权。', 409)
                t['prompts'] = prompts
                t['approval'] = {'id': str(uuid.uuid4()), 'document_hash': t['document']['source_hash'],
                    'plan_version': t['plan']['version'], 'plan_hash': digest(t['plan']),
                    'prompt_hash': prompt_hash(t), 'checkpoint': checkpoint, 'max_attempts': attempts,
                    'attempts_used': 0, 'confirmed_at': now(),
                    'prompt_source': 'cloud_director_v1' if image_targets else 'ollama_translation_or_user_edit',
                    'prompt_evidence': [] if image_targets else getattr(service.ollama, 'last_prompt_evidence', [])}
                if image_targets:
                    t['approval']['image_target'] = copy.deepcopy(next(iter(image_targets.values())))
                    t['approval']['image_targets'] = copy.deepcopy(image_targets)
                t['_prompting'] = False
                t['status'] = 'approved'
                t['error'] = None
            task, _ = service.update(task_id, save)
            return public(task)
        except Exception as exc:
            # Keep bounded, body-free records only for the explicitly requested
            # background path; failed responses are never saved as prompt text.
            failure_evidence = []
            if provider == 'local' and any(slot.get('director', {}).get('background') for slot in approved['plan']['slots']):
                fields = {'phase', 'request_state', 'result_valid', 'input_sha256', 'error_type'}
                failure_evidence = [{key: value for key, value in item.items() if key in fields}
                                    for item in getattr(service.ollama, 'last_prompt_evidence', [])]
            def failed(t):
                t['_prompting'] = False
                t['approval'] = None
                t['status'] = 'plan_ready'
                t['error'] = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                if failure_evidence:
                    t['prompt_failure_evidence'] = failure_evidence
            service.update(task_id, failed)
            if isinstance(exc, HTTPException):
                raise
            bad(public_error(str(exc)), 422)

    @app.put('/api/tasks/{task_id}/prompts')
    def prompts(task_id: str, body: dict):
        bad('完整生成提示词由服务内部维护，请通过画面描述和导演设置修改方案。', 410)

    @app.post('/api/tasks/{task_id}/generate')
    def generate(task_id: str, body: dict):
        key = str(body.get('idempotency_key', '')).strip()
        if not key or len(key) > 200:
            bad('请提供有效幂等键。')
        def change(t):
            snap = snapshot(t)
            if body.get('version') != snap['plan_version'] or body.get('approval_id') != snap['approval_id']:
                bad('生成授权版本不匹配。', 409)
            fingerprint = digest(snap)
            existing = next((j for j in t['jobs'] if j['kind'] == 'generation' and j.get('idempotency_key') == key), None)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    bad('此幂等键已用于其他授权，请使用新键。', 409)
                if existing['status'] in ('failed', 'cancelled') and any(
                        s['state'] in ('submitting', 'submitted', 'awaiting_agent') for s in existing.get('submissions', [])):
                    if busy(t):
                        bad('任务正在执行，请等待当前任务。', 409)
                    existing.update(status='queued', error=None, message='恢复原作业，只检查已记录请求，不重复提交。')
                    t['cancel_requested'] = False
                    t['status'], t['error'] = 'generating', None
                return existing['id']
            if busy(t):
                bad('任务正在执行，请等待当前任务。', 409)
            # Unresolved intents must never be replaced by another submission key.
            unresolved = [s for j in t['jobs'] if j['kind'] == 'generation' and j.get('fingerprint') == fingerprint
                          for s in j.get('submissions', []) if s['state'] in ('submitting', 'submitted', 'awaiting_agent')]
            if unresolved:
                bad('已有未明确完成的生图请求，请保留原作业核查或重启恢复，不能另建请求。', 409)
            missing = [s for s in t['plan']['slots'] if not any(a['slot_id'] == s['id'] and current_asset(t, a) for a in t['assets'])]
            if len(missing) > t['approval']['max_attempts'] - t['approval']['attempts_used']:
                bad('剩余授权尝试次数不足，请重新确认。', 409)
            t['cancel_requested'] = False
            t['status'], t['error'] = 'generating', None
            job = new_job(t, 'generation', snapshot=snap, fingerprint=fingerprint,
                          approval_id=snap['approval_id'], idempotency_key=key, slots=copy.deepcopy(missing),
                          prompts=copy.deepcopy(t['prompts']), submissions=[])
            t['jobs'].append(job)
            return job['id']
        task, job_id = service.update(task_id, change)
        if next(j for j in task['jobs'] if j['id'] == job_id)['status'] == 'queued':
            service.schedule(task_id, job_id)
        return {'job_id': job_id, 'task': public(task)}

    @app.get('/api/agent-image-requests')
    def agent_image_requests():
        requests = []
        for t in service.store.list():
            for j in t['jobs']:
                if j['status'] != 'awaiting_agent' or not matches(t, j.get('snapshot', {})):
                    continue
                for item in j.get('submissions', []):
                    if item['state'] not in ('awaiting_agent', 'submitted'):
                        continue
                    slot = next(s for s in j['slots'] if s['id'] == item['slot_id'])
                    requests.append({'task_id': t['id'], 'job_id': j['id'], 'slot_id': slot['id'],
                                     'approval_id': j['approval_id'], 'state': item['state'],
                                     'description': slot.get('description', '')[:240]})
        return {'requests': requests}

    @app.post('/api/tasks/{task_id}/agent-image-claim')
    def claim_agent_image(task_id: str, body: dict):
        def change(t):
            j = next((j for j in t['jobs'] if j['id'] == body.get('job_id')), None)
            if not j or j['status'] != 'awaiting_agent' or not matches(t, j['snapshot']) or t.get('cancel_requested'):
                bad('协作请求已失效或被取消。', 409)
            if body.get('approval_id') != j['approval_id'] or j['snapshot'].get('image_target', {}).get('provider') != 'codex':
                bad('协作授权不匹配。', 409)
            item = next((s for s in j['submissions'] if s['slot_id'] == body.get('slot_id')), None)
            if not item or item['state'] != 'awaiting_agent':
                bad('此画面已被接收或不属于此协作请求。', 409)
            item['state'], item['claimed_at'] = 'submitted', now()
            j['message'] = '当前 Codex 会话已接收此画面，等待真实生图返回。'
            return {'request_id': item['request_id'], 'job_id': j['id'], 'slot_id': item['slot_id']}
        _, result = service.update(task_id, change)
        return result

    @app.post('/api/tasks/{task_id}/agent-image-result')
    async def complete_agent_image(task_id: str, job_id: str = Form(...), slot_id: str = Form(...),
                                   approval_id: str = Form(...), image: UploadFile = File(...)):
        contents = await image.read(40 * 1024 * 1024 + 1)
        if not contents or len(contents) > 40 * 1024 * 1024:
            bad('返回图片超过大小限制。', 413)
        with service.asset_lock(task_id, job_id + ':' + slot_id):
            t = service.require(task_id)
            j = next((j for j in t['jobs'] if j['id'] == job_id), None)
            if not j or j.get('approval_id') != approval_id or not matches(t, j.get('snapshot', {})):
                bad('返回图片的授权或版本不匹配。', 409)
            if j['snapshot'].get('image_target', {}).get('provider') != 'codex':
                bad('此作业不是内置生图协作。', 409)
            item = next((s for s in j.get('submissions', []) if s['slot_id'] == slot_id), None)
            if not item:
                bad('图片图位不属于此作业。', 409)
            if item['state'] == 'saved':
                return public(t)
            if item['state'] != 'submitted' or j['status'] != 'awaiting_agent':
                bad('此协作请求尚未被接收或已被取消。', 409)
            slot = next(s for s in j['slots'] if s['id'] == slot_id)
            asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, task_id + ':' + item['request_id'] + ':' + slot_id))
            path = service.directory(task_id) / 'assets' / (asset_id + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                import io
                with Image.open(io.BytesIO(contents)) as actual:
                    if actual.width * actual.height > 16_777_216 or min(actual.size) < 64:
                        bad('返回图片像素尺寸不正确。')
                    actual.load()
                    pixels = actual.convert('RGBA' if actual.mode in ('RGBA', 'LA') or 'transparency' in actual.info else 'RGB')
                    pixels.info.clear()
                    pixels.save(path, format='PNG', pnginfo=None, exif=b'')
            except HTTPException:
                raise
            except Exception:
                bad('返回内容不是可读取的图片。')
            service.save_cloud_result(task_id, job_id, slot, item, path)
            current = service.require(task_id)
            current_job = next(j for j in current['jobs'] if j['id'] == job_id)
            if not any(s['state'] in ('awaiting_agent', 'submitted') for s in current_job['submissions']):
                service.finish(task_id, job_id, 'cancelled' if current.get('cancel_requested') else 'completed',
                               '已取消后续协作；已返回图片保留。' if current.get('cancel_requested') else 'GPT 内置协作图片已保存。')
            return public(service.require(task_id))

    @app.post('/api/tasks/{task_id}/cancel')
    def cancel(task_id: str, body: dict):
        def change(t):
            t['cancel_requested'] = True
            for job in t['jobs']:
                if job['kind'] == 'generation':
                    for item in job.get('submissions', []):
                        if item['state'] == 'awaiting_agent':
                            item['state'] = 'failed'
                            job['attempts'] -= 1
                            if t.get('approval') and t['approval']['id'] == job.get('approval_id'):
                                t['approval']['attempts_used'] -= 1
                if job['status'] == 'awaiting_agent':
                    if any(s['state'] == 'submitted' for s in job.get('submissions', [])):
                        job['message'] = '取消后续协作，已接收的画面等待结果保存。'
                    else:
                        job['status'], job['message'] = 'cancelled', '已取消尚未执行的内置生图协作。'
                if job['status'] == 'queued' and not job.get('submissions'):
                    job['status'] = 'cancelled'
                    job['message'] = '已取消，未提交模型请求。'
                elif job['status'] in ('queued', 'running'):
                    job['message'] = '取消已记录；已提交请求等待结果保存，不会清理共享队列。'
            if not busy(t):
                t['status'] = 'cancelled'
        task, _ = service.update(task_id, change)
        return public(task)

    @app.post('/api/tasks/{task_id}/process')
    def process(task_id: str, body: dict):
        from .postprocess import process_image
        asset_id = body.get('asset_id')
        if not isinstance(asset_id, str) or len(asset_id) > 100:
            bad('图片 ID 无效。')
        scale = body.get('upscale', 2)
        if type(scale) is not int or not 1 <= scale <= 4:
            bad('放大倍数仅支持 1—4。')
        denoise = body.get('denoise', True)
        if not isinstance(denoise, bool):
            bad('denoise 必须是布尔值。')
        with service.asset_lock(task_id, asset_id):
            task = service.require(task_id)
            if busy(task):
                bad('请等待模型任务结束后处理图片。', 409)
            asset = next((a for a in task['assets'] if a['id'] == asset_id), None)
            if not asset or not current_asset(task, asset):
                bad('只能处理当前授权的有效图片。', 409)
            snap = snapshot(task)
            source = service.directory(task_id) / 'assets' / (asset_id + '.png')
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != asset['sha256']:
                bad('原图文件不存在或哈希改变，拒绝处理。', 409)
            version = digest({'source_sha256': asset['sha256'], 'denoise': denoise, 'upscale': scale,
                              'algorithm_version': 'median3-lanczos-v1'})[:24]
            filename = asset_id + '-processed-' + version + '.png'
            destination = source.with_name(filename)
            previous = asset.get('processing_versions', {}).get(version)
            if previous and destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == previous['processing']['sha256']:
                result = copy.deepcopy(previous['processing'])
            else:
                try:
                    result = process_image(source, destination, denoise=denoise, upscale=scale)
                except ValueError as exc:
                    bad(str(exc))
                result['version'] = version
            def change(t):
                if not matches(t, snap):
                    bad('图片处理期间授权发生变化，请重新处理。', 409)
                a = next(a for a in t['assets'] if a['id'] == asset_id)
                a.setdefault('processing_versions', {})[version] = {'filename': filename, 'processing': result}
                a.update(processed_url=f'/api/assets/{task_id}/{asset_id}/processed?version={version}',
                         processed_width=result['width'], processed_height=result['height'], processing=result)
            task, _ = service.update(task_id, change)
            return public(task)

    def validated_selections(task, selections):
        if not isinstance(selections, dict):
            bad('选图格式无效。')
        slots = {s['id']: s for s in task['plan']['slots']} if task.get('plan') else {}
        clean = {}
        for slot_id, selection in selections.items():
            if slot_id not in slots or not isinstance(selection, dict):
                bad('选图包含未知图位。')
            asset = next((a for a in task['assets'] if a['id'] == selection.get('asset_id')), None)
            if not asset or asset['slot_id'] != slot_id or not current_asset(task, asset):
                bad('选中图片不属于当前图位与授权版本。', 409)
            use_processed = selection.get('use_processed', False)
            if not isinstance(use_processed, bool) or (use_processed and not asset.get('processed_url')):
                bad('处理后图片不可用。')
            clean[slot_id] = {'asset_id': asset['id'], 'use_processed': use_processed}
        return clean

    @app.post('/api/tasks/{task_id}/select')
    def select(task_id: str, body: dict):
        def change(t):
            if busy(t):
                bad('请等待当前任务结束再选图。', 409)
            snapshot(t)
            t['selections'] = validated_selections(t, body.get('selections'))
            t['status'] = 'review'
        task, _ = service.update(task_id, change)
        return public(task)

    @app.post('/api/tasks/{task_id}/export')
    def export(task_id: str, body: dict):
        from .documents import export_document
        format = body.get('format')
        key = str(body.get('idempotency_key', '')).strip()
        if body.get('confirm_insert') is not True or format not in ('docx', 'markdown', 'html') or not key or len(key) > 200:
            bad('请确认插图位置并提供有效导出格式与幂等键。')
        with service.store.lock:
            task = service.require(task_id)
            if busy(task):
                bad('任务执行中不能导出。', 409)
            if task.get('plan') and task['plan']['slots'] == [] and task['selections'] == {}:
                # A saved zero-illustration plan performs no model action and requires no generation approval.
                snap = {'document_hash': task['document']['source_hash'], 'plan_version': task['plan']['version'],
                        'plan_hash': digest(task['plan']), 'prompt_hash': digest({}), 'checkpoint': None, 'approval_id': None}
                selections = {}
            else:
                snap = snapshot(task)
                selections = validated_selections(task, task['selections'])
            if not selections and task['plan']['slots']:
                bad('请至少选择一张有效图片后导出。')
            fingerprint = digest({'snapshot': snap, 'format': format, 'selections': selections,
                                  'processed': {a['id']: a.get('processing') for a in task['assets'] if a['id'] in [v['asset_id'] for v in selections.values()]}})
            previous = next((e for e in task['exports'] if e.get('idempotency_key') == key), None)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    bad('此导出幂等键已用于其他选图或版本。', 409)
                return {**previous, 'task': public(task)}
            items = []
            for slot in task['plan']['slots']:
                selection = selections.get(slot['id'])
                if not selection:
                    continue
                asset = next(a for a in task['assets'] if a['id'] == selection['asset_id'])
                path = service.processed_path(task_id, asset) if selection['use_processed'] else service.directory(task_id) / 'assets' / (asset['id'] + '.png')
                if not path.is_file():
                    bad('选中图片文件不存在，请重新生成或处理。', 409)
                path = private_image_copy(path, service.directory(task_id) / 'public-cache')
                items.append({'slot_id': slot['id'], 'paragraph_id': slot['paragraph_id'], 'image_path': path,
                              'caption': slot['caption'], 'alt_text': slot['description'], 'asset_id': asset['id']})
            export_id = str(uuid.uuid4())
            output_dir = service.directory(task_id) / 'exports' / export_id
            source_path = Path(task['source_path']) if task.get('source_path') else None
            if source_path and (not source_path.is_file() or hashlib.sha256(source_path.read_bytes()).hexdigest() != task['document']['source_hash']):
                bad('保存的原稿哈希已改变，拒绝从不匹配的文档导出。', 409)
            try:
                output = Path(export_document(source_path,
                        task['document'], task['plan'], items, output_dir, format))
            except ValueError as exc:
                bad(str(exc))
            if not output.is_file() or output.resolve().parent != output_dir.resolve():
                bad('文档模块没有返回可下载的新文件。', 500)
            if source_path and hashlib.sha256(source_path.read_bytes()).hexdigest() != task['document']['source_hash']:
                bad('文档模块修改了原稿，导出未被发布。', 500)
            # Flatten a uniquely named immutable output; repeated exports cannot overwrite prior download URLs.
            filename = export_id + output.suffix
            target = service.directory(task_id) / 'downloads' / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(output.read_bytes())
            entry = {'url': f'/api/exports/{task_id}/{filename}', 'filename': filename, 'format': format,
                     'idempotency_key': key, 'fingerprint': fingerprint, 'created_at': now(), **snap}
            def saved(t):
                t['exports'].append(entry)
                t['status'] = 'exported'
            task, _ = service.update(task_id, saved)
            return {**entry, 'task': public(task)}

    @app.get('/api/assets/{task_id}/{asset_id}/{variant}')
    def asset_file(task_id: str, asset_id: str, variant: str, version: str | None = None):
        task = service.require(task_id)
        asset = next((a for a in task['assets'] if a['id'] == asset_id), None)
        if not asset or variant not in ('original', 'processed') or (variant == 'processed' and not asset.get('processed_url')):
            bad('图片不存在。', 404)
        path = service.processed_path(task_id, asset, version) if variant == 'processed' else service.directory(task_id) / 'assets' / (asset['id'] + '.png')
        if not path.is_file():
            bad('图片文件不存在。', 404)
        path = private_image_copy(path, service.directory(task_id) / 'public-cache')
        return FileResponse(path, media_type='image/png', headers={'X-Content-SHA256': hashlib.sha256(path.read_bytes()).hexdigest()})

    @app.get('/api/exports/{task_id}/{filename}')
    def export_file(task_id: str, filename: str):
        task = service.require(task_id)
        if not any(e['filename'] == filename for e in task['exports']):
            bad('导出文件不存在。', 404)
        path = service.directory(task_id) / 'downloads' / filename
        if not path.is_file():
            bad('导出文件不存在。', 404)
        source_docx = None
        if task['document']['format'] == 'docx':
            source_path = Path(task['source_path']) if task.get('source_path') else None
            if not source_path or not source_path.is_file():
                bad('旧文件需要重新导出。', 409)
            source_docx = source_path.read_bytes()
            if hashlib.sha256(source_docx).hexdigest() != task['document']['source_hash']:
                bad('旧文件需要重新导出。', 409)
        try:
            path = private_export_copy(path, service.directory(task_id) / 'public-cache', generated_image_hashes(task), source_docx)
        except UnsafeLegacyExport:
            bad('旧文件需要重新导出。', 409)
        return FileResponse(path, filename=filename, headers={'X-Content-SHA256': hashlib.sha256(path.read_bytes()).hexdigest()})

    frontend = ROOT / 'frontend' / 'dist'
    if frontend.is_dir():
        app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
    return app


app = create_app()
