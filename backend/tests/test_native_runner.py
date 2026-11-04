"""Native protocol counterexamples with fixture events; no model requests."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


@pytest.fixture
def native():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'codex_image_runner.py'
    spec = importlib.util.spec_from_file_location('tuxu_native_runner_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def request():
    text = 'A red cup beside one blue book, square framing, no text.'
    return {'prompt': text, 'prompt_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'options': {'size': '1024x1024', 'quality': 'auto'}}


def test_native_child_does_not_inherit_host_tool_pipe_or_api_routing(native):
    inherited = {'HOME': '/owner', 'PATH': '/bin', 'CODEX_HOME': '/owner/.codex',
        'HTTPS_PROXY': 'owner-network-proxy', 'OPENAI_API_KEY': 'private-key',
        'OPENAI_BASE_URL': 'parent-endpoint', 'OPENAI_MODEL': 'parent-model',
        'LATEST_MODEL_BASE_URL': 'parent-endpoint', 'CODEX_APP_TOOLS_PIPE_PATH': 'parent-pipe',
        'CODEX_THREAD_ID': 'parent-thread', 'CODEX_SESSION_ID': 'parent-session',
        'CODEX_INTERNAL_ORIGINATOR_OVERRIDE': 'parent-origin', 'CODEX_APP_RUNTIME_CLI_PATH': 'parent-cli'}
    assert native.native_environment(inherited) == {'HOME': '/owner', 'PATH': '/bin',
        'CODEX_HOME': '/owner/.codex', 'HTTPS_PROXY': 'owner-network-proxy'}


def test_rpc_launch_preserves_effective_feature_table_and_clean_environment(native, monkeypatch, tmp_path):
    captured = {}
    def launch(argv, **options):
        captured.update(argv=argv, **options)
        return SimpleNamespace(stdout=iter(()))
    monkeypatch.setattr(native.subprocess, 'Popen', launch)
    monkeypatch.setenv('CODEX_APP_TOOLS_PIPE_PATH', 'parent-pipe')
    rpc = native.Rpc(Path('/fixed/codex'), tmp_path)
    rpc.reader.join(timeout=1)
    argv = captured['argv']
    assert 'features.code_mode={enabled=false,direct_only_tool_namespaces=["image_gen"]}' in argv
    assert 'code_mode.direct_only_tool_namespaces=["image_gen"]' not in argv
    assert not any(argv[i:i+2] == ['--disable', 'code_mode'] for i in range(len(argv)-1))
    assert 'CODEX_APP_TOOLS_PIPE_PATH' not in captured['env']


@pytest.mark.parametrize('mode', [False, None, {'enabled': False},
    {'enabled': True, 'direct_only_tool_namespaces': ['image_gen']},
    {'enabled': False, 'direct_only_tool_namespaces': ['foreign']}])
def test_native_probe_rejects_ineffective_image_namespace_config(native, monkeypatch, tmp_path, mode):
    class Rpc:
        def __init__(self, *args): pass
        def send(self, value): pass
        def close(self): pass
        def call(self, method, params):
            assert method in ('initialize', 'config/read')

            if method == 'config/read': return {'config': {'features': {
                **{name: False for name in native.DISABLED_FEATURES}, 'code_mode': mode}}}
            return {}
    monkeypatch.setattr(native, 'Rpc', Rpc)
    with pytest.raises(native.RunnerError, match='native_direct_image_configuration_missing'):
        native.prepare_rpc(Path('/fixed/codex'), tmp_path)


def events(revised=None, raw=None):
    stream = io.BytesIO(); Image.new('RGB', (96, 96), 'red').save(stream, format='PNG')
    item = {'type': 'imageGeneration', 'id': 'native-1', 'status': 'completed',
            'result': base64.b64encode(stream.getvalue()).decode(), 'revisedPrompt': revised}
    result = []
    if raw is not None:
        result.append({'method': 'codex/event/raw_response_item', 'params': {'msg': {'item': {
            'type': 'function_call', 'call_id': 'call-native-1', 'name': 'imagegen', 'namespace': 'image_gen', 'arguments': json.dumps({'prompt': raw})}}}})
    result.extend([{'method': 'item/completed', 'params': {'item': item}},
                   {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}])
    return result


def fake_execution(native, monkeypatch, feed):
    class Rpc:
        pending = list(feed)
        calls = []
        def call(self, method, params, **kw):
            self.calls.append((method, params))
            return {'thread': {'id': 'isolated-thread'}} if method == 'thread/start' else {}
        def close(self):
            pass
    rpc = Rpc()
    monkeypatch.setattr(native, 'prepare_rpc', lambda *_: rpc)
    monkeypatch.setattr(native.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='codex-cli 0.166.0'))
    return rpc


def test_matching_native_prompt_and_options_produce_receipt_evidence(native, monkeypatch, tmp_path):
    frozen = request()
    rpc = fake_execution(native, monkeypatch, events(revised=frozen['prompt']))
    evidence = native.generate(Path('/fixed/codex'), frozen, tmp_path)
    assert evidence['revised_prompt_equal'] is True and evidence['native_image_count'] == 1
    assert (tmp_path / 'image.png').exists()
    start = dict(rpc.calls)['thread/start']; turn = dict(rpc.calls)['turn/start']
    assert start['experimentalRawEvents'] is True
    assert '1024x1024' in start['developerInstructions'] and 'auto' in start['developerInstructions']
    assert turn['input'][0]['text'] == frozen['prompt']


def test_missing_native_tool_input_evidence_fails_closed(native, monkeypatch, tmp_path):
    fake_execution(native, monkeypatch, events())
    with pytest.raises(native.RunnerError, match='native_prompt_evidence_unavailable'):
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    assert not (tmp_path / 'image.png').exists()


@pytest.mark.parametrize('changed', ['revised', 'raw'])
def test_rewritten_native_tool_prompt_is_rejected(native, monkeypatch, tmp_path, changed):
    frozen = request()
    feed = events(revised='An altered scene' if changed == 'revised' else None,
                  raw='An altered scene' if changed == 'raw' else frozen['prompt'])
    fake_execution(native, monkeypatch, feed)
    with pytest.raises(native.RunnerError, match='frozen_prompt_changed'):
        native.generate(Path('/fixed/codex'), frozen, tmp_path)


def test_raw_native_prompt_exact_match_is_accepted_without_revised_prompt(native, monkeypatch, tmp_path):
    frozen = request()
    fake_execution(native, monkeypatch, events(raw=frozen['prompt']))
    evidence = native.generate(Path('/fixed/codex'), frozen, tmp_path)
    assert evidence['observed_tool_prompt_hashes'] == [frozen['prompt_sha256']]


def test_non_image_tool_item_stops_execution(native, monkeypatch, tmp_path):
    feed = [{'method': 'item/started', 'params': {'item': {'type': 'commandExecution', 'command': 'forbidden'}}}]
    fake_execution(native, monkeypatch, feed)
    with pytest.raises(native.RunnerError, match='unexpected_tool_rejected'):
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    evidence = json.loads((tmp_path / 'execution-evidence.json').read_text())
    assert evidence['rejected_event'] == {'source': 'thread_item', 'kind': 'commandExecution'}
    assert 'forbidden' not in json.dumps(evidence)


def test_user_message_lifecycle_echo_does_not_count_as_tool(native, monkeypatch, tmp_path):
    frozen = request()
    echo = {'type': 'userMessage', 'id': 'user-1',
            'content': [{'type': 'text', 'text': frozen['prompt'], 'text_elements': []}]}
    feed = [{'method': method, 'params': {'item': echo}} for method in ('item/started', 'item/completed')]
    fake_execution(native, monkeypatch, feed + events(revised=frozen['prompt']))
    assert native.generate(Path('/fixed/codex'), frozen, tmp_path)['native_image_count'] == 1
    evidence = json.loads((tmp_path / 'execution-evidence.json').read_text())
    assert evidence['unknown_tool_events'] == 0


@pytest.mark.parametrize('content', [[], [{'type': 'image', 'url': 'private'}],
    [{'type': 'text', 'text': 'changed'}],
    [{'type': 'text', 'text': request()['prompt']}, {'type': 'text', 'text': 'extra'}]])
def test_changed_or_extra_user_input_fails_closed(native, monkeypatch, tmp_path, content):
    fake_execution(native, monkeypatch, [{'method': 'item/completed', 'params': {
        'item': {'type': 'userMessage', 'content': content}}}])
    with pytest.raises(native.RunnerError, match='frozen_user_input_changed'):
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    assert not (tmp_path / 'image.png').exists()


def test_unknown_event_diagnostic_never_persists_arbitrary_text(native, monkeypatch, tmp_path):
    fake_execution(native, monkeypatch, [{'method': 'item/started', 'params': {
        'item': {'type': 'private-content-secret', 'arguments': 'secret'}}}])
    with pytest.raises(native.RunnerError):
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    evidence = (tmp_path / 'execution-evidence.json').read_text()
    assert 'secret' not in evidence and 'unrecognized' in evidence


@pytest.mark.parametrize('kind', ['custom_tool_call', 'local_shell_call', 'tool_search_call',
    'web_search_call', 'function_call_output', 'custom_tool_call_output', 'tool_search_output',
    'additional_tools', 'other', 'future_unknown_tool'])
@pytest.mark.parametrize('method', ['rawResponseItem/completed', 'codex/event/raw_response_item'])
def test_non_native_raw_events_cannot_be_misclassified_not_started(native, monkeypatch, tmp_path, kind, method):
    item = {'type': kind, 'name': 'private-name', 'arguments': 'private-arguments'}
    params = {'item': item} if method == 'rawResponseItem/completed' else {'msg': {'item': item}}
    fake_execution(native, monkeypatch, [{'method': method, 'params': params},
        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}])
    with pytest.raises(native.RunnerError) as raised:
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    assert not isinstance(raised.value, native.NotStartedError)
    evidence = (tmp_path / 'execution-evidence.json').read_text()
    assert json.loads(evidence)['unknown_tool_events'] == 1
    assert 'private-' not in evidence


def test_tool_declarations_and_reasoning_config_do_not_execute_tools(native, monkeypatch, tmp_path):
    frozen = request()
    declarations = {'type': 'additional_tools', 'role': 'developer', 'tools': [
        {'type': 'namespace', 'name': 'image_gen', 'tools': [{'type': 'function', 'name': 'generation'}]}]}
    feed = [{'method': 'rawResponseItem/completed', 'params': {'item': item}}
            for item in (declarations, {'type': 'configuration_update', 'reasoning': {'effort': 'high'}})]
    fake_execution(native, monkeypatch, feed + events(revised=frozen['prompt']))
    assert native.generate(Path('/fixed/codex'), frozen, tmp_path)['native_image_count'] == 1
    evidence = json.loads((tmp_path / 'execution-evidence.json').read_text())
    assert evidence['tool_declaration_events'] == 1 and evidence['unknown_tool_events'] == 0


def test_foreign_call_after_declaration_is_still_rejected(native, monkeypatch, tmp_path):
    feed = [{'method': 'rawResponseItem/completed', 'params': {'item': item}} for item in (
        {'type': 'additional_tools', 'role': 'developer', 'tools': [{'type': 'function', 'name': 'exec'}]},
        {'type': 'function_call', 'name': 'exec', 'arguments': '{}'})]
    fake_execution(native, monkeypatch, feed)
    with pytest.raises(native.RunnerError, match='unexpected_tool_rejected'):
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    evidence = json.loads((tmp_path / 'execution-evidence.json').read_text())
    assert evidence['unknown_tool_events'] == 1 and evidence['raw_function_calls'] == 1


@pytest.mark.parametrize('changed', [False, True])
def test_namespaced_native_custom_call_validates_exact_frozen_input(native, monkeypatch, tmp_path, changed):
    frozen = request()
    call = {'type': 'custom_tool_call', 'namespace': 'image_gen', 'name': 'generation',
            'call_id': 'call-native-1', 'input': json.dumps({'prompt': 'changed' if changed else frozen['prompt']})}
    feed = [{'method': 'rawResponseItem/completed', 'params': {'item': call}}] + events()
    fake_execution(native, monkeypatch, feed)
    if changed:
        with pytest.raises(native.RunnerError, match='frozen_prompt_changed'):
            native.generate(Path('/fixed/codex'), frozen, tmp_path)
    else:
        evidence = native.generate(Path('/fixed/codex'), frozen, tmp_path)
        assert evidence['observed_tool_prompt_hashes'] == [frozen['prompt_sha256']]


@pytest.mark.parametrize('output_type', ['function_call_output', 'custom_tool_call_output'])
@pytest.mark.parametrize('output_id', ['call-native-1', 'foreign'])
def test_native_call_output_is_paired_before_receipt_import(native, monkeypatch, tmp_path, output_type, output_id):
    frozen = request(); feed = events(raw=frozen['prompt'])
    feed.insert(1, {'method': 'rawResponseItem/completed', 'params': {'item': {
        'type': output_type, 'call_id': output_id, 'output': 'private-output'}}})
    fake_execution(native, monkeypatch, feed)
    if output_id == 'foreign':
        with pytest.raises(native.RunnerError, match='unexpected_tool_rejected'):
            native.generate(Path('/fixed/codex'), frozen, tmp_path)
    else:
        assert native.generate(Path('/fixed/codex'), frozen, tmp_path)['native_image_count'] == 1
        evidence = json.loads((tmp_path / 'execution-evidence.json').read_text())
        assert evidence['native_output_events'] == 1 and evidence['native_call_count'] == 1
        assert evidence['observed_tool_prompt_hashes'] == [frozen['prompt_sha256']]
        assert 'private-output' not in json.dumps(evidence)


def test_two_native_call_ids_are_rejected_even_before_two_image_items(native, monkeypatch, tmp_path):
    frozen = request();feed=events(raw=frozen['prompt'])
    duplicate=json.loads(json.dumps(feed[0]));duplicate['params']['msg']['item']['call_id']='call-native-2'
    feed.insert(1,duplicate);fake_execution(native,monkeypatch,feed)
    with pytest.raises(native.RunnerError,match='multiple_native_generations_rejected'):
        native.generate(Path('/fixed/codex'),frozen,tmp_path)


def test_view_image_function_is_not_native_image_generation(native, monkeypatch, tmp_path):
    frozen = request()
    feed = [{'method': 'codex/event/raw_response_item', 'params': {'msg': {'item': {
        'type': 'function_call', 'name': 'view_image', 'arguments': json.dumps({'prompt': frozen['prompt']})}}}}]
    fake_execution(native, monkeypatch, feed)
    with pytest.raises(native.RunnerError, match='unexpected_tool_rejected'):
        native.generate(Path('/fixed/codex'), frozen, tmp_path)


@pytest.mark.parametrize('returncode,stdout,stderr,allowed', [
    (0, '', 'Logged in using ChatGPT\n', True),
    (0, 'Logged in using ChatGPT\n', '', True),
    (1, '', 'Logged in using ChatGPT\n', False),
    (0, '', 'Logged in using an API key\n', False),
    (0, '', 'Logged in using ChatGPT access token\n', False),
])
def test_native_login_probe_only_accepts_existing_chatgpt_mode(
    native, monkeypatch, tmp_path, returncode, stdout, stderr, allowed
):
    monkeypatch.setenv('OPENAI_API_KEY', 'api-key-must-not-be-inherited')
    def login(argv, **options):
        assert argv == ['/fixed/codex', '-c', 'model_provider="openai"', 'login', 'status']
        assert options['cwd'] == tmp_path and options['shell'] is False
        assert 'OPENAI_API_KEY' not in options['env']
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(native.subprocess, 'run', login)
    if allowed:
        native.verify_chatgpt_login(Path('/fixed/codex'), tmp_path)
    else:
        with pytest.raises(native.RunnerError, match='chatgpt_login_required'):
            native.verify_chatgpt_login(Path('/fixed/codex'), tmp_path)


def test_mcp_names_are_disabled_and_verified_before_any_thread(native, monkeypatch, tmp_path):
    instances = []
    class Rpc:
        def __init__(self, binary, cwd, names=()):
            self.names, self.closed = names, False
            instances.append(self)
        def call(self, method, params):
            assert method in ('initialize', 'config/read')
            if method == 'config/read':
                return {'config': {'features': {**{name: False for name in native.DISABLED_FEATURES},
                                   'code_mode': {'enabled': False, 'direct_only_tool_namespaces': ['image_gen']}},
                                   'mcp_servers': {'private-server': {'enabled': bool(not self.names)}}}}
            return {}
        def send(self, value):
            pass
        def close(self):
            self.closed = True
    checks = []
    def login(binary, cwd):
        assert len(instances) == 2 and instances[0].closed
        checks.append((binary, cwd))
    monkeypatch.setattr(native, 'Rpc', Rpc)
    monkeypatch.setattr(native, 'verify_chatgpt_login', login)
    rpc = native.prepare_rpc(Path('/fixed/codex'), tmp_path)
    assert checks == [(Path('/fixed/codex'), tmp_path)]
    assert rpc.names == ('private-server',)


def test_generation_name_requires_image_gen_namespace(native):
    assert native.native_image_call({'namespace': 'image_gen', 'name': 'generation'})
    assert native.native_image_call({'namespace': 'image_gen', 'name': 'imagegen'})
    assert not native.native_image_call({'name': 'generation'})
    assert not native.native_image_call({'namespace': 'foreign', 'name': 'imagegen'})
    assert not native.native_image_call({'name': 'imagegen'})


def test_completed_turn_with_no_tool_has_durable_not_started_evidence(native, monkeypatch, tmp_path):
    fake_execution(native, monkeypatch, [{'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}])
    frozen = request(); frozen['request_id'] = 'request-1'
    with pytest.raises(native.NotStartedError):
        native.generate(Path('/fixed/codex'), frozen, tmp_path)
    evidence = json.loads((tmp_path / 'execution-evidence.json').read_text())
    assert evidence['turn_status'] == 'completed' and evidence['request_id'] == 'request-1'
    assert all(evidence[key] == 0 for key in ('image_generation_count', 'raw_image_events', 'raw_function_calls', 'unknown_tool_events'))


def test_legacy_image_marker_prevents_claiming_not_started(native, monkeypatch, tmp_path):
    fake_execution(native, monkeypatch, [
        {'method': 'codex/event/image_generation_begin', 'params': {'msg': {'type': 'image_generation_begin'}}},
        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}])
    with pytest.raises(native.RunnerError) as raised:
        native.generate(Path('/fixed/codex'), request(), tmp_path)
    assert not isinstance(raised.value, native.NotStartedError)
