"""Durable, bounded one-click orchestration. All model/image effects use existing actions."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import threading
import uuid

from fastapi import HTTPException

ACTIVE = frozenset({'running', 'waiting'})
STATUSES = ACTIVE | {'completed', 'failed', 'cancelled', 'paused'}
STAGES = frozenset({'analysis', 'approval', 'generation', 'visual_review', 'selection', 'export', 'completed'})
MESSAGES = {
    'analysis': '正在阅读全文，规划不超过授权数量的配图。',
    'approval': '正在使用本地模型准备推荐画风与画面描述。',
    'generation': '正在按已冻结授权生成图片，每个图位最多尝试一次。',
    'selection': '全部当前图片已返回，正在选用原图。',
    'visual_review': '正在使用本地视觉模型逐张检查图片，不会自动重画。',
    'visual_attention': '图片检查需要人工处理；已保留原图，请查看检查结果后继续。',
    'export': '正在生成带图文档。',
    'completed': '全部图片与文档已生成，请检查图片内容和文档排版。',
    'zero': '文章无需新增插图，已保留原文并导出文档。',
    'cancelled': '一键流程已停止；已提交图片仍保留结果，不再自动选图或导出。',
    'failed': '一键流程未全部完成，已停止；已返回图片保留，不会自动重试或缺图导出。',
    'prompt_failed': '画面描述准备失败，尚未提交生图。已保留分析方案，请切换人工设置，检查画面描述后重新确认。',
    'unknown': '执行结果尚不能确认，已暂停；请核查原任务，不会重新提交。',
    'changed': '文章或模型配置已改变，一键流程已暂停，请切换人工模式检查。',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def base_hash(task):
    return digest({key: task[key] for key in ('document', 'genre', 'style', 'title')})


class OneClick:
    def __init__(self, service):
        self.service = service
        self.actions = {}
        self.context = threading.local()
        self.stop_event = threading.Event()
        self.thread = None
        self.pump_lock = threading.Lock()
        self.events_lock = threading.Lock()
        self.events = {}

    def bind(self, **actions):
        self.actions = actions

    def _event(self, task_id, run_id):
        with self.events_lock:
            key = (task_id, run_id)
            return self.events.setdefault(key, threading.Event())

    def signal_cancel(self, task_id):
        # Called before taking Store.lock, including during an export/model callback.
        with self.events_lock:
            for (current, _run), event in self.events.items():
                if current == task_id:
                    event.set()

    def current(self):
        return getattr(self.context, 'run', None)

    @contextmanager
    def internal(self, task_id, run_id):
        before = self.current()
        self.context.run = (task_id, run_id)
        try:
            yield
        finally:
            self.context.run = before

    def guard(self, task):
        automation = task.get('automation') or {}
        current = self.current()
        if current is None:
            if automation.get('status') in ACTIVE:
                raise HTTPException(409, '一键流程正在执行，请先停止或切换人工模式。')
            return
        expected = (task['id'], automation.get('id'))
        if (current != expected or automation.get('status') not in ACTIVE or task.get('cancel_requested') or
                self.stop_event.is_set() or self._event(*current).is_set() or
                automation.get('_consent', {}).get('base_hash') != base_hash(task)):
            raise HTTPException(409, '一键执行范围已失效或停止。')

    def effect(self, task, stage, **values):
        """Same transaction as the effect: restart can identify an already committed action."""
        if self.current() is None:
            return
        self.guard(task)
        automation = task['automation']
        action = automation['_actions'].get(stage)
        if automation['stage'] != stage or not action or action['state'] != 'intent':
            raise HTTPException(409, '一键步骤已改变。')
        action.update(state='recorded', **values)

    def job_metadata(self):
        return {'automation_id': self.current()[1]} if self.current() else {}

    @staticmethod
    def cancel_marker(task):
        automation = task.get('automation')
        if automation and automation.get('status') in ACTIVE:
            automation.update(status='cancelled', message=MESSAGES['cancelled'])

    def _target(self, body, *, check_ready=True):
        provider = body['image_provider']
        if provider == 'local':
            if check_ready:
                health = self.service.comfy.health()
                if not health.get('available') or body['checkpoint'] not in health.get('checkpoints', []):
                    raise HTTPException(409, '本地生图模型尚未就绪。')
            return {'provider': 'local', 'checkpoint': body['checkpoint'], 'endpoint': getattr(self.service.comfy, 'url', None)}
        result = {'targets': {aspect: self.service.cloud_config.target(provider, aspect, body['cloud_quality'])
                              for aspect in ('landscape', 'square', 'portrait')}}
        if provider == 'codex':
            state = self.service.local_agent.status()
            if not state.get('available') or not state.get('enabled'):
                raise HTTPException(409, '本机智能体尚未连接或启用。')
            result['agent_revision'] = self.service.local_agent.revision()
        elif check_ready:
            catalog = self.service.cloud_config.list_public()
            profile = next((profile for profile in catalog['profiles'] if profile['id'] == provider), {})
            verification = profile.get('verification') or {}
            if verification.get('connection') == 'failed' or verification.get('generation') in ('rejected', 'unknown'):
                raise HTTPException(409, '所选生图服务仍有待处理的失败或未知状态。')
        return result

    def _check_scope(self, task):
        automation = task['automation']
        consent = automation['_consent']
        generation = automation['_actions'].get('generation') or {}
        # After dispatch the worker follows the accepted request. A busy ComfyUI
        # may time out on health probes without changing the approved target.
        check_ready = automation['stage'] in ('analysis', 'approval', 'generation') and generation.get('state') != 'recorded'
        return (base_hash(task) == consent['base_hash'] and
                self._target(consent['body'], check_ready=check_ready) == consent['target'] and
                ('builtin_agent' not in consent or
                 self.service.builtin_agent.snapshot(consent['body']['model']) == consent['builtin_agent']))

    def check_approved_target(self, task, targets, agent_revision=None):
        if self.current() is None:
            return
        self.guard(task)
        frozen = task['automation']['_consent']['target']
        if targets:
            for slot in task['plan']['slots']:
                if targets.get(slot['id']) != frozen.get('targets', {}).get(slot['aspect_ratio']):
                    raise HTTPException(409, '一键目标配置已改变。')
        if 'agent_revision' in frozen and frozen['agent_revision'] != agent_revision:
            raise HTTPException(409, '一键智能体配置已改变。')
        consent = task['automation']['_consent']
        if ('builtin_agent' in consent and
                self.service.builtin_agent.snapshot(task['plan']['model']) != consent['builtin_agent']):
            raise HTTPException(409, '内置智能体设置已改变，请重新确认。')

    def start_run(self, task_id, body):
        from .models import TEXT_MODEL
        allowed = {'confirm', 'idempotency_key', 'model', 'image_provider', 'checkpoint', 'cloud_quality', 'format', 'max_images'}
        if set(body) - allowed or body.get('confirm') is not True:
            raise HTTPException(400, '请明确确认一键执行范围。')
        value = {'model': body.get('model') or TEXT_MODEL, 'image_provider': body.get('image_provider', 'local'),
                 'checkpoint': body.get('checkpoint', ''), 'cloud_quality': body.get('cloud_quality', 'medium'),
                 'format': body.get('format', 'docx'), 'max_images': body.get('max_images', 5)}
        key = body.get('idempotency_key')
        if (not isinstance(key, str) or not key.strip() or len(key) > 200 or
                not isinstance(value['model'], str) or not 1 <= len(value['model']) <= 150 or
                value['image_provider'] not in ('local', 'ark', 'openai', 'codex') or
                not isinstance(value['checkpoint'], str) or len(value['checkpoint']) > 300 or
                value['cloud_quality'] not in ('medium', 'high') or value['format'] not in ('docx', 'markdown', 'html') or
                type(value['max_images']) is not int or not 1 <= value['max_images'] <= 5):
            raise HTTPException(400, '一键执行参数不正确。')
        body_hash = digest(value)
        task = self.service.require(task_id)
        existing = task.get('automation')
        if existing:
            if existing.get('_key') == key and existing.get('_body_hash') == body_hash:
                return task
            raise HTTPException(409, '此任务已建立一键执行记录，请保留原记录或使用人工模式。')
        health = self.service.ollama.health()
        models = health.get('models', [health.get('model')])
        if not health.get('available') or value['model'] not in models:
            raise HTTPException(409, '所选本地文章理解模型尚未就绪。')
        try:
            target = self._target(value)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(409, '所选生图服务尚未就绪。') from None
        run_id = str(uuid.uuid4())
        builtin_profile = self.service.builtin_agent.snapshot(value['model'])
        def begin(current):
            previous = current.get('automation')
            if previous:
                if previous.get('_key') == key and previous.get('_body_hash') == body_hash:
                    return
                raise HTTPException(409, '已有一键执行记录。')
            if (current['status'] != 'imported' or current.get('plan') is not None or current.get('assets') or
                    current.get('approval') or current.get('jobs') or current.get('exports') or current.get('cancel_requested')):
                raise HTTPException(409, '一键模式仅适用于刚导入且尚未制作的原稿。')
            current['automation'] = {'id': run_id, 'status': 'running', 'stage': 'analysis', 'message': MESSAGES['analysis'],
                'format': value['format'], 'max_images': value['max_images'], 'completed_images': 0, 'total_images': 0,
                '_key': key, '_body_hash': body_hash, '_actions': {}, '_consent': {
                    'base_hash': base_hash(current), 'article_sha256': current['document']['source_hash'],
                    'body': copy.deepcopy(value), 'target': target, 'one_attempt_per_slot': True,
                    'builtin_agent': builtin_profile, 'preparation_failure_protocol': 1}}
        current, _ = self.service.update(task_id, begin)
        self._event(task_id, current['automation']['id'])
        return current

    def _update(self, task_id, run_id, change):
        with self.internal(task_id, run_id):
            def update(task):
                self.guard(task)
                change(task, task['automation'])
            return self.service.update(task_id, update)[0]

    def _halt(self, task_id, run_id, status, message):
        event = self._event(task_id, run_id)
        def halt(task):
            automation = task.get('automation') or {}
            # The cancel route signals before it can acquire the store lock.
            # Leave the terminal status to that route if it already signaled.
            if event.is_set() or automation.get('id') != run_id or automation.get('status') not in ACTIVE:
                return False
            automation.update(status=status, message=message)
            task['cancel_requested'] = True
            return True
        _, changed = self.service.update(task_id, halt)
        if changed:
            event.set()
            self.actions['cancel'](task_id, {})

    def _invoke(self, task_id, run_id, stage, body):
        with self.internal(task_id, run_id):
            def intent(task, automation):
                if automation['stage'] != stage or stage in automation['_actions']:
                    raise HTTPException(409, '一键步骤已提交。')
                automation['_actions'][stage] = {'state': 'intent'}
            self._update(task_id, run_id, intent)
            try:
                self.actions[stage](task_id, body)
            except HTTPException as exc:
                if not self._record_prompt_failure(task_id, run_id, stage, exc):
                    raise

    def _record_prompt_failure(self, task_id, run_id, stage, error):
        """A confirmed local preparation rejection is distinct from an unknown image effect."""
        if stage != 'approval' or error.status_code != 422:
            return False
        try:
            if not self._check_scope(self.service.require(task_id)):
                return False
        except Exception:
            return False
        def failed(task):
            self.guard(task)
            run = task['automation']
            action = run['_actions'].get(stage) or {}
            if (run['stage'] != stage or action.get('state') != 'intent' or task.get('_prompting') or
                    task.get('_prompt_preparation_failure') != {
                        'kind': 'model_validation', 'run_id': run_id, 'plan_hash': digest(task.get('plan'))} or
                    task.get('approval') or not task.get('plan') or task.get('status') != 'plan_ready' or
                    not task.get('error') or task.get('assets') or task.get('exports') or
                    any(job.get('kind') != 'analysis' for job in task.get('jobs', [])) or
                    ('builtin_agent' in run['_consent'] and
                     self.service.builtin_agent.snapshot(task['plan']['model']) != run['_consent']['builtin_agent'])):
                return False
            action.update(state='failed', code='prompt_preparation_failed')
            run.update(status='failed', message=MESSAGES['prompt_failed'])
            task.update(status='failed', error=MESSAGES['prompt_failed'])
            return True
        try:
            _, handled = self.service.update(task_id, failed)
        except HTTPException:
            return False
        if handled:
            self._event(task_id, run_id).set()
        return handled

    def _advance(self, task_id, run_id, stage):
        self._update(task_id, run_id, lambda _task, automation: automation.update(stage=stage, status='running', message=MESSAGES[stage]))

    def step(self, task):
        from .main import busy, current_asset, unresolved
        automation = task['automation']
        task_id, run_id, stage = task['id'], automation['id'], automation['stage']
        if task.get('cancel_requested'):
            self.cancel_marker(task)
            self.service.update(task_id, self.cancel_marker)
            return
        try:
            scope_current = self._check_scope(task)
        except Exception:
            scope_current = False
        if not scope_current:
            self._halt(task_id, run_id, 'paused', MESSAGES['changed'])
            return
        action = automation['_actions'].get(stage)
        body = automation['_consent']['body']
        if action and action['state'] == 'intent':
            self._halt(task_id, run_id, 'paused', MESSAGES['unknown'])
            return
        if stage == 'analysis':
            if not action:
                self._invoke(task_id, run_id, stage, {'model': body['model']})
                return
            job = next(job for job in task['jobs'] if job['id'] == action['job_id'])
            if job['status'] in ('queued', 'running'):
                self._update(task_id, run_id, lambda _task, run: run.update(status='waiting'))
                return
            if job['status'] != 'completed' or not task.get('plan'):
                self._halt(task_id, run_id, 'failed', MESSAGES['failed'])
                return
            count = len(task['plan']['slots'])
            self._update(task_id, run_id, lambda _task, run: run.update(total_images=count))
            self._advance(task_id, run_id, 'approval' if count else 'export')
        elif stage == 'approval':
            if not action:
                self._invoke(task_id, run_id, stage, {'version': task['plan']['version'], 'confirm': True,
                    'image_provider': body['image_provider'], 'checkpoint': body['checkpoint'],
                    'cloud_quality': body['cloud_quality'], 'max_attempts': len(task['plan']['slots'])})
                return
            if not task.get('approval') or task['approval']['id'] != action['approval_id']:
                self._halt(task_id, run_id, 'paused', MESSAGES['unknown'])
                return
            self._advance(task_id, run_id, 'generation')
        elif stage == 'generation':
            if not action:
                self._invoke(task_id, run_id, stage, {'version': task['plan']['version'], 'approval_id': task['approval']['id'],
                    'idempotency_key': 'one-click-gen:' + run_id})
                return
            job = next(job for job in task['jobs'] if job['id'] == action['job_id'])
            assets = {asset['slot_id'] for asset in task['assets'] if current_asset(task, asset)}
            self._update(task_id, run_id, lambda _task, run: run.update(completed_images=sum(slot['id'] in assets for slot in task['plan']['slots'])))
            submissions = job.get('submissions', [])
            if any(item.get('bridge_state') == 'unknown' for item in submissions) or job['status'] in ('failed', 'cancelled') and unresolved(task):
                self._halt(task_id, run_id, 'paused', MESSAGES['unknown'])
            elif job['status'] in ('failed', 'cancelled') or any(item.get('state') in ('failed', 'not_started') for item in submissions):
                self._halt(task_id, run_id, 'failed', MESSAGES['failed'])
            elif busy(task) or unresolved(task):
                self._update(task_id, run_id, lambda _task, run: run.update(status='waiting'))
            elif all(slot['id'] in assets for slot in task['plan']['slots']):
                profile = (task.get('approval') or {}).get('_builtin_agent') or {}
                self._advance(task_id, run_id, 'visual_review' if profile.get('visual_review_enabled') else 'selection')
            else:
                self._halt(task_id, run_id, 'failed', MESSAGES['failed'])
        elif stage == 'visual_review':
            report = self.service.builtin_agent.summary(task)
            if report['status'] == 'passed':
                self._advance(task_id, run_id, 'selection')
            elif report['status'] in ('needs_attention', 'unavailable', 'interrupted'):
                self._halt(task_id, run_id, 'paused', MESSAGES['visual_attention'])
            else:
                pending = [asset for asset in task['assets'] if current_asset(task, asset)]
                if any((asset.get('_builtin_visual_review') or {}).get('status') in ('queued', 'running') for asset in pending):
                    self._update(task_id, run_id, lambda _task, run: run.update(status='waiting'))
                    return
                for slot in task['plan']['slots']:
                    asset = next((asset for asset in pending if asset['slot_id'] == slot['id']), None)
                    if asset and not asset.get('_builtin_visual_review'):
                        self.service.builtin_agent.request_review(task_id, asset['id'], confirm=True, automated=True)
                        return
                self._halt(task_id, run_id, 'paused', MESSAGES['visual_attention'])
        elif stage == 'selection':
            if not action:
                selections = {}
                for slot in task['plan']['slots']:
                    asset = next((asset for asset in task['assets'] if asset['slot_id'] == slot['id'] and current_asset(task, asset)), None)
                    if not asset:
                        self._halt(task_id, run_id, 'failed', MESSAGES['failed'])
                        return
                    selections[slot['id']] = {'asset_id': asset['id'], 'use_processed': False}
                self._invoke(task_id, run_id, stage, {'selections': selections})
                return
            self._advance(task_id, run_id, 'export')
        elif stage == 'export':
            if not action:
                self._invoke(task_id, run_id, stage, {'format': body['format'], 'confirm_insert': True,
                    'idempotency_key': 'one-click-export:' + run_id})
                return
            self._update(task_id, run_id, lambda current, run: run.update(status='completed', stage='completed',
                message=MESSAGES['completed'] if current['plan']['slots'] else MESSAGES['zero'], export_url=action['url']))

    def recover(self):
        for task in self.service.store.list():
            automation = task.get('automation') or {}
            if automation.get('status') not in ACTIVE:
                continue
            run_id = automation['id']
            action = automation['_actions'].get(automation['stage'])
            if action and action['state'] == 'intent':
                self._halt(task['id'], run_id, 'paused', MESSAGES['unknown'])
            elif automation['stage'] == 'analysis' and action:
                job = next((job for job in task['jobs'] if job['id'] == action['job_id']), None)
                if job and job['status'] == 'running':
                    def interrupted(current):
                        present = next(job for job in current['jobs'] if job['id'] == action['job_id'])
                        present.update(status='failed', error=MESSAGES['unknown'], message=MESSAGES['unknown'])
                    self.service.update(task['id'], interrupted)
                    self._halt(task['id'], run_id, 'paused', MESSAGES['unknown'])

    def pump(self):
        if not self.pump_lock.acquire(blocking=False):
            return
        try:
            for task in self.service.store.list():
                automation = task.get('automation') or {}
                if self.stop_event.is_set() or automation.get('status') not in ACTIVE:
                    continue
                try:
                    self.step(task)
                except Exception:
                    current = self.service.require(task['id']).get('automation') or {}
                    if current.get('id') == automation['id'] and current.get('status') in ACTIVE:
                        self._halt(task['id'], automation['id'], 'paused', MESSAGES['unknown'])
        finally:
            self.pump_lock.release()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                self.pump()
                self.stop_event.wait(.12)
        self.thread = threading.Thread(target=loop, daemon=True, name='one-click-coordinator')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
