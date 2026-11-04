#!/usr/bin/env python3
"""One-shot Codex native-image runner; no API keys, cloud text API or shell.

Reads the tuxu-local-image-v1 request from stdin, runs one ephemeral app-server
turn, and accepts only an observed native image-generation result. A start marker
prevents this directory from invoking the model again after an interruption.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image

INSTRUCTIONS = (
    'You are a single-use image execution worker. The user message is the complete, frozen, '
    'approved image prompt. Call the native image generation tool exactly once, passing the '
    'user message verbatim as the prompt. Do not translate, expand, summarize or rewrite it. '
    'Use no shell, file, web, browser, MCP, external service, subagent or other tool. '
    'Do not read any files or credentials. Generate one new image, no reference images. '
    'If the native image tool is unavailable, stop and say unavailable. Never create a '
    'placeholder, SVG, code drawing or substitute image. After the native tool completes, stop.'
)
DISABLED_FEATURES = ('shell_tool', 'unified_exec', 'multi_agent', 'multi_agent_v2',
    'code_mode_host', 'apps', 'enable_mcp_apps', 'plugins', 'plugin_discovery_prefetch',
    'skill_search', 'skill_mcp_dependency_install', 'view_image', 'shell_snapshot',
    'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser', 'sleep_tool')


def native_environment(inherited=None):
    """A native child must not attach to its parent agent's tool pipe or routing."""
    inherited = os.environ if inherited is None else inherited
    blocked = {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'LATEST_MODEL_BASE_URL',
               'M2M2_MODEL', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT',
               'ARK_API_KEY', 'MINIMAX_API_KEY', 'ANTHROPIC_API_KEY'}
    return {key: value for key, value in inherited.items()
            if key not in blocked and (not key.startswith('CODEX_') or key == 'CODEX_HOME')}


class RunnerError(Exception):
    pass


class NotStartedError(RunnerError):
    """A completed native turn with durable evidence that no tool was invoked."""


def validate_user_echo(item, prompt):
    """The server echoes turn input as a userMessage, not a tool invocation."""
    content = item.get('content')
    if (not isinstance(content, list) or len(content) != 1 or
            not isinstance(content[0], dict) or content[0].get('type') != 'text' or
            content[0].get('text') != prompt):
        raise RunnerError('frozen_user_input_changed')


def reject_tool(trace, source, kind):
    # Persist protocol names only, never a tool's arguments, output or model prose.
    known = {'exec_command_begin', 'mcp_tool_call_begin', 'dynamic_tool_call_request',
             'web_search_begin', 'patch_apply_begin', 'collab_agent_spawn_begin',
             'commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall',
             'collabAgentToolCall', 'webSearch', 'imageView', 'sleep', 'hookPrompt',
             'functionCallOutput', 'plan', 'function_call', 'custom_tool_call',
             'local_shell_call', 'tool_search_call', 'web_search_call',
             'function_call_output', 'custom_tool_call_output', 'tool_search_output',
             'additional_tools', 'other'}
    trace['unknown_tool_events'] += 1
    trace['rejected_event'] = {'source': source, 'kind': kind if kind in known else 'unrecognized'}
    raise RunnerError('unexpected_tool_rejected')


def native_image_call(item):
    namespace, name = item.get('namespace'), item.get('name', '')
    if namespace == 'image_gen' and name in ('imagegen', 'generation'):
        return True
    return namespace in (None, '') and name in (
        'image_gen.imagegen', 'image_gen.generation', 'image_gen__imagegen', 'image_gen__generation')


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def image_bytes(item):
    # Prefer image bytes carried by a native protocol item. A native savedPath is
    # accepted only from that item, never from the assistant's prose/final answer.
    value = item.get('result')
    if isinstance(value, str) and value:
        if value.startswith('data:image/') and ';base64,' in value:
            value = value.split(';base64,', 1)[1]
        try:
            contents = base64.b64decode(value, validate=True)
            if contents:
                return contents
        except (ValueError, TypeError):
            pass
    saved = item.get('savedPath')
    if isinstance(saved, str):
        path = Path(saved)
        if path.is_absolute() and path.is_file() and not path.is_symlink() and path.stat().st_size <= 40 * 1024 * 1024:
            return path.read_bytes()
    raise RunnerError('native_image_bytes_unavailable')


def save_pixels(contents, destination):
    if not contents or len(contents) > 40 * 1024 * 1024:
        raise RunnerError('invalid_native_image')
    with Image.open(io.BytesIO(contents)) as source:
        if min(source.size) < 64 or source.width * source.height > 16_777_216:
            raise RunnerError('invalid_native_image_dimensions')
        source.load()
        pixels = source.convert('RGBA' if source.mode in ('RGBA', 'LA') or 'transparency' in source.info else 'RGB')
    pixels.info.clear()
    pixels.save(destination, format='PNG', pnginfo=None, exif=b'')
    destination.chmod(0o600)


class Rpc:
    def __init__(self, binary, cwd, mcp_names=()):
        argv = [str(binary), '-c', 'model_provider="openai"', '-c', 'model="gpt-6.1-sol"',
                '-c', 'model_reasoning_effort="high"', '-c', 'mcp_servers={}',
                '-c', 'web_search="disabled"', '-c', 'features.skip_host_skill_discovery=true',
                '-c', 'features.code_mode={enabled=false,direct_only_tool_namespaces=["image_gen"]}',
                '--enable', 'image_generation']
        for feature in DISABLED_FEATURES:
            argv.extend(['--disable', feature])
        if mcp_names:
            disabled = '{' + ','.join(json.dumps(name) + '={enabled=false}' for name in mcp_names) + '}'
            argv.extend(['-c', 'mcp_servers=' + disabled])
        argv.extend(['app-server', '--stdio'])
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', cwd=cwd, shell=False,
            env=native_environment())
        self.inbox = queue.Queue(maxsize=200)
        self.counter = 0
        self.pending = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 64 * 1024 * 1024:
                    raise RunnerError('oversize_native_event')
                self.inbox.put(json.loads(line))
        except Exception:
            pass
        self.inbox.put(None)

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def receive(self, timeout=10):
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise RunnerError('native_timeout')
        if item is None:
            raise RunnerError('native_server_stopped')
        # Refuse all interactive tool/command approval requests. There is no
        # browser-driven command execution or interactive credential collection.
        if 'method' in item and 'id' in item:
            self.send({'jsonrpc': '2.0', 'id': item['id'], 'error': {'code': -32601, 'message': 'Unsupported request'}})
        return item

    def call(self, method, params, timeout=30):
        self.counter += 1
        number = self.counter
        self.send({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.receive(max(.1, deadline - time.monotonic()))
            if item.get('id') == number:
                if 'error' in item:
                    raise RunnerError('native_rpc_rejected')
                return item.get('result', {})
            self.pending.append(item)
        raise RunnerError('native_timeout')

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()


def verify_chatgpt_login(binary, work):
    # Login status checks the active local credential mode. The app-server
    # account endpoint may wait for remote account metadata even without refresh.
    try:
        result = subprocess.run(
            [str(binary), '-c', 'model_provider="openai"', 'login', 'status'],
            env=native_environment(), cwd=work, capture_output=True, text=True,
            timeout=10, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError('chatgpt_login_required') from exc
    if result.returncode != 0 or (result.stdout + result.stderr).strip() != 'Logged in using ChatGPT':
        raise RunnerError('chatgpt_login_required')


def prepare_rpc(binary, work):
    """Inspect only local account type/config; never start a model turn here."""
    names = ()
    for _ in range(2):
        rpc = Rpc(binary, work, names)
        try:
            rpc.call('initialize', {'clientInfo': {'name': 'tuxu_native_image_runner', 'version': '0.2.0'},
                                   'capabilities': {'experimentalApi': True}})
            rpc.send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
            effective = rpc.call('config/read', {'includeLayers': False}).get('config', {})
            servers = effective.get('mcp_servers') or {}
            flags = effective.get('features', {})
            if any(flags.get(feature) is not False for feature in DISABLED_FEATURES):
                raise RunnerError('native_tool_configuration_not_isolated')
            mode = flags.get('code_mode')
            if (not isinstance(mode, dict) or mode.get('enabled') is not False or
                    mode.get('direct_only_tool_namespaces') != ['image_gen']):
                raise RunnerError('native_direct_image_configuration_missing')
            if all(isinstance(value, dict) and value.get('enabled') is False for value in servers.values()):
                verify_chatgpt_login(binary, work)
                return rpc
            names = tuple(servers)
        except Exception:
            rpc.close()
            raise
        rpc.close()
    raise RunnerError('mcp_configuration_not_isolated')


def generate(binary, request, work, timeout=540):
    version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=5, shell=False)
    matched = re.search(r'codex-cli (\d+)\.(\d+)\.(\d+)', version.stdout)
    if not matched or tuple(map(int, matched.groups())) < (0, 166, 0):
        raise RunnerError('codex_version_unsupported')
    rpc = prepare_rpc(binary, work)
    image_ids, completed, raw_prompt_hashes, native_calls = set(), {}, [], set()
    revised_prompt_equal = None
    trace = {'request_id': request.get('request_id'), 'prompt_sha256': request.get('prompt_sha256'),
             'turn_status': None, 'image_generation_count': 0, 'raw_image_events': 0,
             'raw_function_calls': 0, 'unknown_tool_events': 0}
    try:
        execution_instructions = INSTRUCTIONS + '\nApproved execution options (do not add to or rewrite the prompt): ' + json.dumps(request.get('options', {}), sort_keys=True) + '. Match the approved framing. Quality auto means the native default, not a promised quality tier.'
        started = rpc.call('thread/start', {'cwd': str(work), 'ephemeral': True, 'approvalPolicy': 'never',
            'sandbox': 'read-only', 'model': 'gpt-6.1-sol', 'modelProvider': 'openai',
            'experimentalRawEvents': True,
            'baseInstructions': INSTRUCTIONS, 'developerInstructions': execution_instructions,
            'config': {'model_reasoning_effort': 'high', 'mcp_servers': {}, 'web_search': 'disabled'}})
        thread_id = started['thread']['id']
        rpc.call('turn/start', {'threadId': thread_id, 'effort': 'high',
            'input': [{'type': 'text', 'text': request['prompt'], 'text_elements': []}]})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = rpc.pending.pop(0) if rpc.pending else rpc.receive(min(15, max(.1, deadline - time.monotonic())))
            except RunnerError as exc:
                if str(exc) == 'native_timeout':
                    continue
                raise
            method, params = event.get('method'), event.get('params', {})
            legacy = params.get('msg') if isinstance(params.get('msg'), dict) else {}
            if legacy.get('type') in ('image_generation_begin', 'image_generation_end'):
                trace['raw_image_events'] += 1
            if legacy.get('type') in ('exec_command_begin', 'mcp_tool_call_begin', 'dynamic_tool_call_request',
                                       'web_search_begin', 'patch_apply_begin', 'collab_agent_spawn_begin'):
                reject_tool(trace, 'legacy', legacy['type'])
            if method in ('item/started', 'item/completed'):
                item = params.get('item', {})
                if item.get('type') == 'imageGeneration':
                    image_ids.add(item.get('id'))
                    if len(image_ids) > 1:
                        raise RunnerError('multiple_native_generations_rejected')
                    if method == 'item/completed':
                        completed[item['id']] = item
                elif item.get('type') == 'userMessage':
                    validate_user_echo(item, request['prompt'])
                elif item.get('type') not in ('agentMessage', 'reasoning', 'contextCompaction'):
                    reject_tool(trace, 'thread_item', item.get('type'))
            raw_item = None
            if method in ('rawResponseItem/completed', 'codex/event/raw_response_item'):
                raw_item = params.get('item') or legacy.get('item')
            elif legacy.get('type') == 'raw_response_item':
                raw_item = legacy.get('item')
            if isinstance(raw_item, dict):
                item = raw_item
                if item.get('type') == 'image_generation_call':
                    trace['raw_image_events'] += 1
                elif item.get('type') in ('function_call', 'custom_tool_call'):
                    trace['raw_function_calls'] += 1
                    try:
                        args = json.loads(item.get('arguments' if item['type'] == 'function_call' else 'input', '{}'))
                    except (ValueError, TypeError):
                        args = {}
                    if not native_image_call(item):
                        reject_tool(trace, 'raw_function', item['type'])
                    call_id = item.get('call_id')
                    if not isinstance(call_id, str) or not call_id:
                        raise RunnerError('native_call_identity_missing')
                    native_calls.add(call_id)
                    if len(native_calls) > 1:
                        raise RunnerError('multiple_native_generations_rejected')
                    if isinstance(args, dict) and isinstance(args.get('prompt'), str):
                        raw_prompt_hashes.append(hashlib.sha256(args['prompt'].encode()).hexdigest())
                        if args['prompt'] != request['prompt']:
                            raise RunnerError('frozen_prompt_changed')
                elif item.get('type') in ('function_call_output', 'custom_tool_call_output'):
                    # Outputs do not invoke anything. Accept only the receipt of
                    # the single native call already observed in this same turn.
                    if item.get('call_id') not in native_calls:
                        reject_tool(trace, 'raw_item', item['type'])
                    trace['native_output_events'] = trace.get('native_output_events', 0) + 1
                elif item.get('type') == 'additional_tools':
                    # Protocol tool definitions are configuration, not invocations.
                    # Actual function/tool calls still pass the strict native-only gate.
                    if item.get('role') != 'developer' or not isinstance(item.get('tools'), list):
                        reject_tool(trace, 'raw_item', 'additional_tools')
                    trace['tool_declaration_events'] = trace.get('tool_declaration_events', 0) + 1
                elif item.get('type') not in ('message', 'agent_message', 'reasoning', 'compaction',
                                               'context_compaction', 'compaction_trigger', 'configuration_update'):
                    reject_tool(trace, 'raw_item', item.get('type'))
            if method == 'turn/completed':
                status = params.get('turn', {}).get('status')
                trace['turn_status'] = status
                if status != 'completed':
                    raise RunnerError('native_turn_failed')
                break
        else:
            raise RunnerError('native_timeout')
        if len(completed) != 1:
            if (trace['turn_status'] == 'completed' and not image_ids and
                    trace['raw_image_events'] == trace['raw_function_calls'] == trace['unknown_tool_events'] == 0):
                raise NotStartedError('native_image_tool_unavailable')
            raise RunnerError('native_image_tool_unavailable')
        item = next(iter(completed.values()))
        if item.get('failure') or item.get('status') not in ('completed', 'success', 'succeeded'):
            raise RunnerError('native_image_failed')
        revised = item.get('revisedPrompt')
        if revised is not None:
            revised_prompt_equal = revised == request['prompt']
            if not revised_prompt_equal:
                raise RunnerError('frozen_prompt_changed')
        if not raw_prompt_hashes and revised_prompt_equal is not True:
            raise RunnerError('native_prompt_evidence_unavailable')
        save_pixels(image_bytes(item), work / 'image.png')
        return {'native_item_id': item['id'], 'native_image_count': len(image_ids),
                'codex_version': '.'.join(matched.groups()), 'revised_prompt_equal': revised_prompt_equal,
                'observed_tool_prompt_hashes': raw_prompt_hashes}
    finally:
        trace['image_generation_count'] = len(image_ids)
        trace['native_call_count'] = len(native_calls)
        trace['observed_tool_prompt_hashes'] = raw_prompt_hashes
        atomic_json(work / 'execution-evidence.json', trace)
        rpc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex', required=True, type=Path)
    parser.add_argument('--probe', action='store_true', help='Check account type and disabled tools without a model turn')
    args = parser.parse_args()
    work = Path.cwd().resolve()
    if args.probe:
        try:
            rpc = prepare_rpc(args.codex.resolve(), work)
            rpc.close()
            print(json.dumps({'available': True, 'mode': 'codex-native', 'account_type': 'chatgpt',
                              'configured_tool_restrictions_verified': True, 'mcp_servers_disabled': True,
                              'model_turns': 0}))
            return 0
        except Exception as exc:
            code = str(exc) if isinstance(exc, RunnerError) else 'native_probe_failed'
            print(json.dumps({'available': False, 'mode': 'codex-native', 'code': code, 'model_turns': 0}))
            return 1
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise RunnerError('oversize_request')
        request = json.loads(raw)
        if (request.get('protocol') != 'tuxu-local-image-v1' or not isinstance(request.get('prompt'), str) or
                not request['prompt'] or len(request['prompt']) > 6000 or
                hashlib.sha256(request['prompt'].encode()).hexdigest() != request.get('prompt_sha256')):
            raise RunnerError('invalid_request')
        marker = os.open(work / 'started.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(marker, 'w') as stream:
            json.dump({'request_id': request['request_id'], 'prompt_sha256': request['prompt_sha256']}, stream)
        evidence = generate(args.codex.resolve(), request, work)
        atomic_json(work / 'result.json', {'status': 'completed', 'mode': 'codex-native',
            'request_id': request['request_id'], 'prompt_sha256': request['prompt_sha256'], 'image': 'image.png',
            'evidence': evidence})
        return 0
    except Exception as exc:
        # Fixed code only; exceptions, model output and prompts never reach stdout.
        code = str(exc) if isinstance(exc, RunnerError) else 'runner_interrupted_or_invalid'
        atomic_json(work / 'runner-status.json', {'status': 'not_started' if isinstance(exc, NotStartedError) else 'unknown',
            'code': code, 'request_id': request.get('request_id') if 'request' in locals() else None,
            'prompt_sha256': request.get('prompt_sha256') if 'request' in locals() else None})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
