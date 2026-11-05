"""Small durable local-model role runner; no cloud keys or image generation tools."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .models import TEXT_MODEL
from .vision_review import (CATEGORIES, MAX_IMAGE_BYTES, ReviewInterrupted, ReviewUnavailable,
                            VisionReviewAdapter, image_input, parse_checks, visible_brief)

ROLES = ('article_director', 'prompt_author', 'visual_reviewer')
LIMITS = {'max_images': 5, 'generation_attempts_per_image': 1,
          'visual_reviews_per_image': 1, 'text_repairs_per_step': 1}
STATUSES = ('queued', 'running', 'passed', 'needs_attention', 'unavailable', 'interrupted')
ACTIVE = frozenset(('queued', 'running'))
REVIEW_SPEC = 'tuxu-visual-review-v1'
SETTING = 'builtin_agent_config'
SUGGESTIONS = {
    'subject': '请核对主体是否符合描述，必要时调整主体描述后重新授权。',
    'count': '请核对物体数量，必要时在导演台明确数量后重新授权。',
    'placement': '请核对物体摆放，必要时调整导演台位置后重新授权。',
    'style': '请核对画风与明暗，必要时调整风格后重新授权。',
    'text': '请核对画面内文字或伪字，必要时调整描述后重新授权。',
    'unavailable': '本地图片检查未完成，请人工查看图片。',
    'interrupted': '图片检查已中断，请人工查看图片；不会自动重复检查。',
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _model(value):
    if (not isinstance(value, str) or len(value) > 128 or '://' in value or
            value.startswith(('sk-', 'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_')) or
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+\-]*', value) is None):
        raise HTTPException(400, '本地模型名称无效。')
    return value


def _revision(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def public_review(record):
    """Project only known enums and server-authored suggestions, even for old data."""
    if not isinstance(record, dict):
        return None
    status = record.get('status') if record.get('status') in STATUSES else 'unavailable'
    checks = []
    if status in ('passed', 'needs_attention'):
        try:
            checks = parse_checks({'checks': record.get('checks')})
            status = 'passed' if all(item['verdict'] == 'match' for item in checks) else 'needs_attention'
        except ReviewUnavailable:
            status = 'unavailable'
    suggestions = [SUGGESTIONS[item['category']] for item in checks if item['verdict'] != 'match']
    if status in ('unavailable', 'interrupted'):
        suggestions = [SUGGESTIONS[status]]
    try:
        model = _model((record.get('binding') or {}).get('model'))
    except (HTTPException, AttributeError):
        model = TEXT_MODEL
    return {'status': status, 'checks': checks, 'suggestions': suggestions, 'model': model}


def _asset_is_current(task, asset):
    # Import only after Service construction; keep the authorization comparison
    # centralized as new frozen approval fields are added by the application.
    from .main import current_asset
    return current_asset(task, asset)


class BuiltinAgentRuntime:
    def __init__(self, service, vision_adapter=None):
        self.service = service
        self.vision = vision_adapter
        self.events = {}
        self.events_lock = threading.Lock()
        self.stopped = threading.Event()

    def _default(self):
        value = {'schema_version': 1, 'model': TEXT_MODEL, 'visual_review_enabled': False}
        return {**value, 'revision': _digest(value)}

    def _read_config(self, connection=None):
        if connection is None:
            with self.service.store.lock, self.service.store._connect() as conn:
                return self._read_config(conn)
        row = connection.execute('SELECT value FROM settings WHERE key=?', (SETTING,)).fetchone()
        if row is None:
            return self._default()
        try:
            value = json.loads(row[0])
            if (not isinstance(value, dict) or set(value) != {'schema_version', 'model', 'visual_review_enabled', 'revision'} or
                    type(value['schema_version']) is not int or value['schema_version'] != 1 or
                    type(value['visual_review_enabled']) is not bool or not _revision(value['revision'])):
                raise ValueError
            _model(value['model'])
            return value
        except (ValueError, TypeError, KeyError, HTTPException):
            raise HTTPException(409, '内置智能体配置无效，请检查本地配置。') from None

    def catalog(self):
        value = self._read_config()
        return {'runtime': 'tuxu-builtin', **{key: value[key] for key in ('model', 'visual_review_enabled', 'revision')},
                'roles': list(ROLES), 'limits': copy.deepcopy(LIMITS)}

    def configure(self, body):
        if (not isinstance(body, dict) or set(body) != {'model', 'visual_review_enabled', 'expected_revision'} or
                type(body.get('visual_review_enabled')) is not bool or not _revision(body.get('expected_revision'))):
            raise HTTPException(400, '内置智能体设置格式无效。')
        model = _model(body['model'])
        with self.service.store.lock, self.service.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = self._read_config(conn)
            if body['expected_revision'] != previous['revision']:
                raise HTTPException(409, '内置智能体设置已改变，请刷新后重试。')
            if (model, body['visual_review_enabled']) == (previous['model'], previous['visual_review_enabled']):
                return self.catalog()
            value = {'schema_version': 1, 'model': model, 'visual_review_enabled': body['visual_review_enabled'],
                     'revision': _digest({'nonce': uuid.uuid4().hex})}
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (SETTING, json.dumps(value)))
        return self.catalog()

    def snapshot(self, model=None):
        value = self._read_config()
        return {**value, 'model': _model(model if model is not None else value['model'])}

    def analyze(self, document, genre, style, model, *, locale="zh"):
        options = {"locale": locale} if locale == "en" else {}
        return self.service.ollama.analyze(document, genre, style, _model(model), **options)

    def prompts(self, slots, style, model):
        return self.service.ollama.prompts(slots, style, _model(model))

    @staticmethod
    def busy(task):
        return any((asset.get('_builtin_visual_review') or {}).get('status') in ACTIVE for asset in task.get('assets', []))

    def signal_cancel(self, task_id):
        with self.events_lock:
            for (current, _review), event in self.events.items():
                if current == task_id:
                    event.set()

    def stop(self):
        self.stopped.set()
        with self.events_lock:
            for event in self.events.values():
                event.set()

    def _event(self, task_id, review_id):
        with self.events_lock:
            return self.events.setdefault((task_id, review_id), threading.Event())

    def _binding(self, task, asset, *, automated):
        if (task.get('cancel_requested') or task.get('status') == 'cancelled' or self.stopped.is_set() or
                not _asset_is_current(task, asset)):
            raise ReviewInterrupted('task_changed')
        plan, approval = task['plan'], task['approval']
        slot = next((slot for slot in plan['slots'] if slot['id'] == asset['slot_id']), None)
        if slot is None:
            raise ReviewInterrupted('slot_changed')
        model = _model(plan['model'])
        current = self.snapshot(model)
        if automated:
            profile = approval.get('_builtin_agent')
            run = task.get('automation') or {}
            if (not isinstance(profile, dict) or profile != current or not profile.get('visual_review_enabled') or
                    run.get('status') not in ('running', 'waiting') or run.get('stage') != 'visual_review'):
                raise ReviewInterrupted('automatic_scope_changed')
        brief = visible_brief(slot)
        return {'spec': REVIEW_SPEC, 'asset_id': asset['id'], 'slot_id': asset['slot_id'], 'model': model,
                'asset_sha256': asset['sha256'], 'approval_id': approval['id'],
                'document_hash': task['document']['source_hash'], 'plan_hash': _digest(plan),
                'prompt_hash': approval['prompt_hash'], 'brief_hash': _digest(brief),
                'config_revision': current['revision'], 'approval_profile_hash': _digest(approval.get('_builtin_agent')),
                'automated': automated, 'automation_id': (task.get('automation') or {}).get('id') if automated else None}

    def request_review(self, task_id, asset_id, *, confirm=True, automated=False):
        if confirm is not True or type(automated) is not bool:
            raise HTTPException(400, '请明确确认本地图片检查。')
        def reserve(task):
            asset = next((item for item in task['assets'] if item['id'] == asset_id), None)
            if asset is None:
                raise HTTPException(404, '图片不存在。')
            try:
                binding = self._binding(task, asset, automated=automated)
            except (ReviewInterrupted, ReviewUnavailable, KeyError):
                raise HTTPException(409, '当前图片或授权已改变，无法开始检查。') from None
            previous = asset.get('_builtin_visual_review')
            if isinstance(previous, dict) and (previous.get('binding') or {}).get('approval_id') == binding['approval_id']:
                if previous.get('binding') != binding:
                    previous.update(status='interrupted', checks=[], reason='changed', updated_at=_now())
                return None
            if task.get('_prompting') or any(job.get('status') in ('queued', 'running', 'awaiting_agent') for job in task.get('jobs', [])) or self.busy(task):
                raise HTTPException(409, '任务正在执行，请稍后检查图片。')
            if not automated and (task.get('automation') or {}).get('status') in ('running', 'waiting'):
                raise HTTPException(409, '一键流程正在执行，请先切换人工模式。')
            record = {'id': str(uuid.uuid4()), 'status': 'queued', 'checks': [], 'binding': binding,
                      'created_at': _now(), 'updated_at': _now()}
            asset['_builtin_visual_review'] = record
            self._event(task_id, record['id'])
            return record['id']
        task, review_id = self.service.update(task_id, reserve)
        if review_id:
            try:
                self.service.executor.submit(self._run, task_id, asset_id, review_id)
            except Exception:
                self._terminal(task_id, asset_id, review_id, 'unavailable', reason='executor_unavailable')
                task = self.service.require(task_id)
        return task

    def _matches(self, task, asset, record):
        if self._event(task['id'], record['id']).is_set() or self.stopped.is_set():
            return False
        try:
            return record['binding'] == self._binding(task, asset, automated=record['binding']['automated'])
        except Exception:
            return False

    def _captured_image(self, task_id, asset):
        # Only an existing internal UUID asset path; never a caller-supplied path/URL.
        try:
            if str(uuid.UUID(task_id)) != task_id or str(uuid.UUID(asset['id'])) != asset['id']:
                raise ValueError
            base = self.service.store.directory / 'tasks' / task_id
            path = base / 'assets' / (asset['id'] + '.png')
            if any(part.is_symlink() for part in (base, base / 'assets', path)):
                raise ValueError
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as file:
                metadata = os.fstat(file.fileno())
                if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_IMAGE_BYTES:
                    raise ValueError
                source = file.read(MAX_IMAGE_BYTES + 1)
            return image_input(source, asset['sha256'])
        except ReviewInterrupted:
            raise
        except Exception:
            raise ReviewUnavailable('image_unavailable') from None

    def _guard(self, task_id, asset_id, review_id):
        task = self.service.require(task_id)
        asset = next((item for item in task['assets'] if item['id'] == asset_id), {})
        record = asset.get('_builtin_visual_review') or {}
        if record.get('id') != review_id or record.get('status') != 'running' or not self._matches(task, asset, record):
            raise ReviewInterrupted('scope_changed')
        # Catch a disk change before the initial call and before an optional repair.
        self._captured_image(task_id, asset)

    def _terminal(self, task_id, asset_id, review_id, status, *, checks=None, evidence=None, reason=None):
        def save(task):
            asset = next((item for item in task['assets'] if item['id'] == asset_id), None)
            record = (asset or {}).get('_builtin_visual_review') or {}
            if record.get('id') != review_id or record.get('status') not in ACTIVE:
                return
            if not self._matches(task, asset, record):
                status_to_save, safe_checks, safe_evidence = 'interrupted', [], {}
            else:
                status_to_save, safe_checks, safe_evidence = status, checks or [], evidence or {}
                # Verify the original bytes again at the publication boundary.
                try:
                    self._captured_image(task_id, asset)
                except Exception:
                    status_to_save, safe_checks, safe_evidence = 'interrupted', [], {}
            # Cancellation is signalled before waiting for Store.lock. It can
            # arrive while the original-file check above is still reading.
            if not self._matches(task, asset, record):
                status_to_save, safe_checks, safe_evidence = 'interrupted', [], {}
            record.update(status=status_to_save, checks=safe_checks, evidence=safe_evidence,
                          reason=reason, updated_at=_now())
        self.service.update(task_id, save)

    def _run(self, task_id, asset_id, review_id):
        try:
            with self.service.model_lock:
                def start(task):
                    asset = next((item for item in task['assets'] if item['id'] == asset_id), {})
                    record = asset.get('_builtin_visual_review') or {}
                    if record.get('id') != review_id or record.get('status') != 'queued':
                        return None
                    if not self._matches(task, asset, record):
                        record.update(status='interrupted', checks=[], updated_at=_now())
                        return None
                    record.update(status='running', model_request='intent', updated_at=_now())
                    return copy.deepcopy(asset)
                task, asset = self.service.update(task_id, start)
                if asset is None:
                    return
                encoded, image_evidence = self._captured_image(task_id, asset)
                slot = next(slot for slot in task['plan']['slots'] if slot['id'] == asset['slot_id'])
                adapter = self.vision or VisionReviewAdapter(getattr(self.service.ollama, 'url', 'http://127.0.0.1:11434'))
                result = adapter.review(task['plan']['model'], visible_brief(slot), encoded,
                                        before_call=lambda: self._guard(task_id, asset_id, review_id))
                checks = parse_checks({'checks': result.get('checks')})
                status = 'passed' if all(item['verdict'] == 'match' for item in checks) else 'needs_attention'
                # Adapter evidence is private, but even it gets a numeric allowlist.
                evidence = {'image': image_evidence}
                info = result.get('evidence') or {}
                if type(info.get('model_calls')) is int and 1 <= info['model_calls'] <= 2:
                    evidence['model_calls'] = info['model_calls']
                evidence['calls'] = [{key: item[key] for key in ('prompt_eval_count', 'eval_count', 'total_duration')
                                      if type(item.get(key)) is int and 0 <= item[key] <= 10**15}
                                     for item in info.get('calls', [])[:2] if isinstance(item, dict)]
                self._terminal(task_id, asset_id, review_id, status, checks=checks, evidence=evidence)
        except ReviewInterrupted:
            self._terminal(task_id, asset_id, review_id, 'interrupted', reason='scope_changed')
        except Exception:
            self._terminal(task_id, asset_id, review_id, 'unavailable', reason='local_review_unavailable')

    def summary(self, task):
        try:
            assets = [asset for asset in task.get('assets', []) if _asset_is_current(task, asset)]
            slots = (task.get('plan') or {}).get('slots', [])
            states = []
            for slot in slots:
                asset = next((item for item in assets if item['slot_id'] == slot['id']), None)
                record = (asset or {}).get('_builtin_visual_review') or {}
                if not asset or not record:
                    states.append('waiting')
                elif not self._matches(task, asset, record):
                    states.append('interrupted')
                else:
                    states.append(public_review(record)['status'])
            status = 'passed'
            for failure in ('interrupted', 'unavailable', 'needs_attention'):
                if failure in states:
                    status = failure
                    break
            else:
                if any(state in ('waiting', 'queued', 'running') for state in states):
                    status = 'waiting'
            return {'status': status, 'total_images': len(slots),
                    'completed_images': sum(state == 'passed' for state in states)}
        except Exception:
            return {'status': 'interrupted', 'total_images': 0, 'completed_images': 0}

    def recover(self):
        # A persisted model intent may already have executed. Never infer non-execution.
        for task in self.service.store.list():
            if not self.busy(task):
                continue
            def interrupt(current):
                for asset in current.get('assets', []):
                    record = asset.get('_builtin_visual_review') or {}
                    if record.get('status') in ACTIVE:
                        record.update(status='interrupted', checks=[], reason='restart', updated_at=_now())
            self.service.update(task['id'], interrupt)
