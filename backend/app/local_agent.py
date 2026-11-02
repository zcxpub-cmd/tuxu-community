"""Opt-in, fixed local runner. Durable claims never become automatic retries.

Configuration and private receipts stay in the Community data directory. The web
API may toggle a configured runner, but cannot choose a program or a path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from .models import ModelError

UNAVAILABLE = '本机智能体未配置或不可用，请先在本机完成设置。'
DISABLED = '本机智能体已配置，启用后才会自动接收已授权的生图任务。'
READY = '已启用本机智能体，确认生成后自动执行并回填真实图片。'
UNKNOWN = '本机智能体执行状态待核查；不会自动重复生图。'
RUNNING = '本机智能体已接收已确认画面，正在自动生图。'
CHANGED = '本机智能体配置已改变，请重新确认当前方案。'
INCOMPATIBLE = 'Codex 版本不兼容，请在本机配置 0.166.0 或更新版本。'
NATIVE_UNAVAILABLE = 'Codex 原生登录或工具检查未通过，请检查本机 ChatGPT 登录状态。'
NOT_STARTED = '本机智能体未能启动图片工具，未生成图片，请检查配置后重新确认。'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


class LocalAgentBridge:
    def __init__(self, service):
        self.service = service
        self.path = service.store.directory / 'local-agent.json'
        self.directory = service.store.directory / 'local-agent-requests'
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None
        self.process_lock = threading.Lock()
        self.version_cache = {}
        self.native_probe_cache = {}
        self.unavailable_message = UNAVAILABLE

    def configuration(self):
        self.unavailable_message = UNAVAILABLE
        try:
            if self.path.stat().st_size > 65536:
                return None
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('mode') not in ('local-runner', 'codex-native'):
                return None
            argv = value.get('argv')
            if (not isinstance(argv, list) or not 1 <= len(argv) <= 16 or
                    any(not isinstance(arg, str) or not arg or len(arg) > 4096 or '\0' in arg for arg in argv)):
                return None
            program = Path(argv[0])
            if not program.is_absolute() or not program.is_file() or not os.access(program, os.X_OK):
                return None
            timeout = value.get('timeout_seconds', 600)
            if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 1 <= timeout <= 1800:
                return None
            if 'label' in value and (not isinstance(value['label'], str) or
                    not re.fullmatch(r'[\w .()（）·+\-]{1,60}', value['label'])):
                return None
            if 'adapter_config' in value:
                from .agent_adapter import load_adapter
                expected = Path(__file__).resolve().parents[2] / 'scripts' / 'local_image_runner.py'
                if (value['mode'] != 'local-runner' or len(argv) != 4 or Path(argv[1]).resolve() != expected or
                        argv[2] != '--config' or argv[3] != value['adapter_config']):
                    return None
                load_adapter(value['adapter_config'])
            # A native mode must use the shipped runner and an absolute Codex binary.
            if value['mode'] == 'codex-native':
                expected = Path(__file__).resolve().parents[2] / 'scripts' / 'codex_image_runner.py'
                if len(argv) != 4 or Path(argv[1]).resolve() != expected or argv[2] != '--codex':
                    return None
                binary = Path(argv[3])
                if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
                    return None
                version_key = (str(binary), binary.stat().st_mtime_ns)
                if version_key not in self.version_cache:
                    try:
                        result = subprocess.run([str(binary), '--version'], shell=False, capture_output=True,
                                                text=True, timeout=3)
                        match = re.search(r'codex-cli (\d+)\.(\d+)\.(\d+)', result.stdout)
                        self.version_cache[version_key] = bool(match and tuple(map(int, match.groups())) >= (0, 166, 0))
                    except (OSError, subprocess.TimeoutExpired):
                        self.version_cache[version_key] = False
                if not self.version_cache[version_key]:
                    self.unavailable_message = INCOMPATIBLE
                    return None
                probe_key = (tuple(argv), expected.stat().st_mtime_ns, version_key)
                cached = self.native_probe_cache.get(probe_key)
                if not cached or cached[0] < time.monotonic():
                    try:
                        result = subprocess.run(argv + ['--probe'], shell=False, capture_output=True,
                            text=True, timeout=25, cwd=self.service.store.directory)
                        probe_value = json.loads(result.stdout)
                        valid = (result.returncode == 0 and probe_value.get('available') is True and
                                 probe_value.get('account_type') == 'chatgpt' and probe_value.get('model_turns') == 0)
                    except (OSError, subprocess.TimeoutExpired, ValueError):
                        valid = False
                    self.native_probe_cache[probe_key] = (time.monotonic() + 120, valid)
                if not self.native_probe_cache[probe_key][1]:
                    self.unavailable_message = NATIVE_UNAVAILABLE
                    return None
            return {**value, 'argv': argv, 'timeout_seconds': timeout}
        except (OSError, ValueError, TypeError):
            return None

    def status(self):
        config = self.configuration()
        available = config is not None
        enabled = available and config.get('enabled') is True
        mode = config['mode'] if available else 'unconfigured'
        label = (config.get('label') or ('Codex 本机智能体' if mode == 'codex-native' else '本机生图智能体')) if available else '本机生图智能体'
        return {'available': available, 'enabled': enabled, 'mode': mode,
                'label': label,
                'message': READY if enabled else DISABLED if available else self.unavailable_message}

    def revision(self, config=None, adapter=None):
        config = config or self.configuration()
        if config is None:
            return 'unconfigured'
        files = []
        for arg in config['argv']:
            path = Path(arg)
            if path.is_absolute() and path.is_file():
                stat = path.stat()
                files.append({'path': str(path), 'size': stat.st_size, 'mtime': stat.st_mtime_ns,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= 2 * 1024 * 1024 else None})
        value = {'mode': config['mode'], 'argv': config['argv'], 'files': files,
                 'timeout_seconds': config['timeout_seconds']}
        if 'adapter_config' in config:
            from .agent_adapter import adapter_bundle
            value['adapter_bundle'] = adapter if adapter is not None else adapter_bundle(config['adapter_config'], config['argv'][0])
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def catalog(self):
        catalog = self.service.cloud_config.list_public()
        state = self.status()
        catalog['built_in'].update(ready=state['available'] and state['enabled'],
                                   label=state['label'], mode=state['mode'], message=state['message'])
        return catalog

    def setup(self, body):
        if not isinstance(body, dict) or set(body) != {'enabled'} or not isinstance(body['enabled'], bool):
            raise ValueError('Only enabled is accepted')
        with self.lock:
            config = self.configuration()
            if config is None:
                if body['enabled']:
                    raise ValueError(UNAVAILABLE)
                return self.status()
            config['enabled'] = body['enabled']
            atomic_json(self.path, config)
        return self.status()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name='local-image-agent')
        self.thread.start()

    @staticmethod
    def _terminate(process):
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            # A wrapper can exit on TERM while a descendant ignores it. The
            # owned process group must be killed even after its leader exits.
            try:
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.poll() is None:
                    process.kill()
            except OSError:
                pass

    def stop(self):
        self.stop_event.set()
        with self.process_lock:
            self._terminate(self.process)
        if self.thread:
            self.thread.join(timeout=4)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.pump()
            except Exception:
                # Raw runner exceptions can contain private prompts, paths or credentials.
                # A failed poll never creates a second submission.
                pass
            self.stop_event.wait(.35)

    def request_directory(self, request_id):
        return self.directory / hashlib.sha256(request_id.encode()).hexdigest()

    def _message(self, task_id, job_id, slot_id, state, message):
        def change(task):
            job = next(j for j in task['jobs'] if j['id'] == job_id)
            item = next(s for s in job['submissions'] if s['slot_id'] == slot_id)
            if item['state'] == 'submitted':
                item['bridge_state'] = state
                job['message'] = message
        self.service.update(task_id, change)

    def _import(self, task_id, job_id, slot_id, item, work):
        receipt = work / 'result.json'
        if not receipt.is_file():
            return False
        try:
            if receipt.stat().st_size > 16384:
                return False
            result = json.loads(receipt.read_text(encoding='utf-8'))
            request = json.loads((work / 'request.json').read_text(encoding='utf-8'))
            task = self.service.require(task_id)
            job = next(j for j in task['jobs'] if j['id'] == job_id)
            frozen = job['prompts'][slot_id]['text']
            expected_hash = hashlib.sha256(frozen.encode()).hexdigest()
            image = work / 'image.png'
            if (result.get('status') != 'completed' or result.get('request_id') != item['request_id'] or
                    request.get('request_id') != item['request_id'] or request.get('approval_id') != job['approval_id'] or
                    request.get('prompt') != frozen or result.get('prompt_sha256') != expected_hash or
                    request.get('prompt_sha256') != expected_hash or result.get('image') != 'image.png' or
                    not image.is_file() or image.is_symlink() or image.stat().st_size > 40 * 1024 * 1024):
                return False
            self.service.complete_agent_image(task_id, {'job_id': job_id, 'slot_id': slot_id,
                'approval_id': request['approval_id']}, image.read_bytes())
            return True
        except Exception:
            return False

    def _not_started(self, task_id, job_id, slot_id, work):
        """Only a new complete, durable, tool-free native turn can refund a claim."""
        try:
            paths = [work / 'runner-status.json', work / 'execution-evidence.json']
            if any(not path.is_file() or path.is_symlink() or path.stat().st_size > 16384 for path in paths):
                return False
            status, evidence = [json.loads(path.read_text(encoding='utf-8')) for path in paths]
            task = self.service.require(task_id)
            job = next(j for j in task['jobs'] if j['id'] == job_id)
            item = next(s for s in job['submissions'] if s['slot_id'] == slot_id)
            expected_hash = hashlib.sha256(job['prompts'][slot_id]['text'].encode()).hexdigest()
            if (job['status'] != 'awaiting_agent' or item['state'] != 'submitted' or not item.get('local_bridge') or
                    item.get('bridge_mode') != 'codex-native' or status.get('status') != 'not_started' or
                    status.get('code') != 'native_image_tool_unavailable' or evidence.get('turn_status') != 'completed' or
                    any(evidence.get(key) != 0 for key in ('image_generation_count', 'raw_image_events',
                        'raw_function_calls', 'unknown_tool_events')) or
                    any(record.get('request_id') != item['request_id'] or record.get('prompt_sha256') != expected_hash
                        for record in (status, evidence))):
                return False
            def terminal(task):
                current = next(j for j in task['jobs'] if j['id'] == job_id)
                target = next(s for s in current['submissions'] if s['slot_id'] == slot_id)
                if target['state'] != 'submitted':
                    raise ValueError('claim changed')
                target['state'], target['bridge_state'] = 'not_started', 'not_started'
                refunds = 1
                for pending in current['submissions']:
                    if pending['state'] == 'awaiting_agent':
                        pending['state'] = 'failed'
                        refunds += 1
                current['attempts'] -= refunds
                if (task.get('approval') or {}).get('id') == current['approval_id']:
                    task['approval']['attempts_used'] -= refunds
            self.service.update(task_id, terminal)
            self.service.finish(task_id, job_id, 'failed', NOT_STARTED)
            return True
        except Exception:
            return False

    def pump(self):
        """One serial sweep; submitted items are receipt-only even after restart."""
        for task in self.service.store.list():
            if self.stop_event.is_set():
                return
            for job in task['jobs']:
                if job['status'] != 'awaiting_agent':
                    continue
                for item in job.get('submissions', []):
                    if item['state'] == 'submitted' and item.get('local_bridge') is True:
                        work = self.request_directory(item['request_id'])
                        if (not self._import(task['id'], job['id'], item['slot_id'], item, work) and
                                not self._not_started(task['id'], job['id'], item['slot_id'], work)):
                            self._message(task['id'], job['id'], item['slot_id'], 'unknown', UNKNOWN)
                    if item['state'] != 'awaiting_agent':
                        continue
                    config = self.configuration()
                    if config is None or config.get('enabled') is not True:
                        continue
                    with self.service.model_lock:
                        self._dispatch(task['id'], job['id'], item['slot_id'], config)

    def _dispatch(self, task_id, job_id, slot_id, config):
        if self.stop_event.is_set():
            return
        identity = {'job_id': job_id, 'slot_id': slot_id}
        task = self.service.require(task_id)
        job = next(j for j in task['jobs'] if j['id'] == job_id)
        adapter = None
        if 'adapter_config' in config:
            from .agent_adapter import adapter_bundle
            adapter = adapter_bundle(config['adapter_config'], config['argv'][0])
        if job.get('snapshot', {}).get('local_agent_revision') != self.revision(config, adapter):
            def rejected(task):
                current = next(j for j in task['jobs'] if j['id'] == job_id)
                for item in current.get('submissions', []):
                    if item['state'] == 'awaiting_agent':
                        item['state'] = 'failed'
                        current['attempts'] -= 1
                        if (task.get('approval') or {}).get('id') == current['approval_id']:
                            task['approval']['attempts_used'] -= 1
            self.service.update(task_id, rejected)
            self.service.finish(task_id, job_id, 'failed', CHANGED)
            return
        identity['approval_id'] = job['approval_id']
        try:
            claimed = self.service.claim_agent_image(task_id, identity, local=True, mode=config['mode'])
        except Exception:
            return
        request_id = claimed['request_id']
        work = self.request_directory(request_id)
        try:
            work.mkdir(parents=True, exist_ok=True, mode=0o700)
            work.chmod(0o700)
            prompt = job['prompts'][slot_id]['text']
            target = job['snapshot']['image_targets'][slot_id]
            request = {'protocol': 'tuxu-local-image-v1', 'request_id': request_id,
                       'approval_id': identity['approval_id'], 'prompt': prompt,
                       'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                       'options': {key: target[key] for key in ('size', 'quality') if key in target}}
            argv = config['argv']
            if adapter is not None:
                # The claimed request executes its approved immutable config copy,
                # never a subsequently reconfigured owner file. The child checks
                # all program hashes again before its exclusive start marker.
                atomic_json(work / 'adapter-config.json', adapter['config'])
                request['_adapter_bundle'] = adapter
                argv = [*argv[:-1], str(work / 'adapter-config.json')]
            atomic_json(work / 'request.json', request)
            # Request data travels over stdin; stdout/stderr are deliberately private
            # and discarded. Never execute a command supplied by a browser request.
            environment = {key: value for key, value in os.environ.items() if key not in {
                'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT',
                'ARK_API_KEY', 'MINIMAX_API_KEY', 'ANTHROPIC_API_KEY'}}
            with self.process_lock:
                if self.stop_event.is_set():
                    self._message(task_id, job_id, slot_id, 'unknown', UNKNOWN)
                    return
                self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, cwd=work, shell=False, env=environment, start_new_session=os.name == 'posix')
            try:
                self.process.communicate(json.dumps(request, ensure_ascii=False).encode(), timeout=config['timeout_seconds'])
            except subprocess.TimeoutExpired:
                self._terminate(self.process)
            finally:
                with self.process_lock:
                    self.process = None
            item = {'request_id': request_id}
            if not self._import(task_id, job_id, slot_id, item, work) and not self._not_started(task_id, job_id, slot_id, work):
                self._message(task_id, job_id, slot_id, 'unknown', UNKNOWN)
        except Exception:
            self._message(task_id, job_id, slot_id, 'unknown', UNKNOWN)
