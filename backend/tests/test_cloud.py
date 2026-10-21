"""Cloud configuration, credential isolation, and one-POST recovery contracts.

All HTTP is mocked. No actual key discovery, model generation, or Store imports.
"""
import base64
import concurrent.futures
import copy
import io
import json

import httpx
from PIL import Image, PngImagePlugin
import pytest

from backend.app import cloud
from backend.app.cloud import CloudConfigStore, CloudImageAdapter, compose_cloud_prompts, credential_origin
from backend.app.director import default_director
from backend.app.models import ModelError, RejectedSubmissionError

KEY = 'KEY_CANARY_NOT_PUBLIC'
PROMPT = 'PRIVATE_PROMPT_CANARY 红色杯子在左、合上蓝书在右。'
REAL_HTTP_CLIENT = httpx.Client


@pytest.fixture
def configured(tmp_path):
    store = CloudConfigStore(tmp_path / 'private')
    store.update('ark', {'api_key': KEY, 'enabled': True})
    return store


def mock_http(monkeypatch, respond):
    original = REAL_HTTP_CLIENT
    transport = httpx.MockTransport(respond)
    settings = []
    def client(**kwargs):
        settings.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != 'timeout'}))
        return original(transport=transport, **kwargs)
    monkeypatch.setattr(httpx, 'Client', client)
    monkeypatch.setattr(cloud.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('8.8.8.8', 443))])
    return settings


def png():
    image = Image.new('RGBA', (128, 96), (150, 40, 60, 200))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('prompt', PROMPT)
    metadata.add_text('api_key', KEY)
    content = io.BytesIO()
    image.save(content, format='PNG', pnginfo=metadata)
    return content.getvalue()


def success(request):
    return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}],
                                   'usage': {'total_tokens': 10, 'input_tokens': 4, 'unknown': KEY}})


def test_public_config_is_safe_ready_not_a_connection_proof_and_file_modes(configured):
    public = configured.list_public()
    ark = public['profiles'][0]
    assert ark['ready'] and ark['key_present'] and ark['enabled']
    assert public['profiles'][1]['ready'] is False
    assert public['built_in'] == {'id': 'codex', 'label': 'GPT 内置 · Codex 协作', 'ready': True, 'mode': 'agent'}
    text = json.dumps(public)
    assert KEY not in text and 'credential_origin' not in text and 'key_file' not in text and 'key_env' not in text
    assert configured.path.stat().st_mode & 0o777 == 0o600
    assert configured.directory.stat().st_mode & 0o777 == 0o700


def test_blank_key_preserves_and_clear_key_removes_source(configured):
    before = configured.target('ark', 'square')
    configured.update('ark', {'api_key': ''})
    assert configured.target('ark', 'square') == before
    result = configured.update('ark', {'clear_key': True})
    assert not result['key_present'] and not result['ready']
    with pytest.raises(ModelError):
        configured.target('ark', 'square')


@pytest.mark.parametrize('source', ['api_key', 'key_file', 'key_env'])
def test_host_change_clears_every_old_key_source_even_when_blank_key_sent(configured, tmp_path, monkeypatch, source):
    data = json.loads(configured.path.read_text())
    profile = data['profiles']['ark']
    profile.pop('api_key')
    profile['credential_origin'] = credential_origin(profile['base_url'])
    if source == 'api_key':
        profile[source] = KEY
    elif source == 'key_file':
        path = tmp_path / 'credential'
        path.write_text(KEY)
        profile[source] = str(path)
    else:
        monkeypatch.setenv('TUXU_CLOUD_TEST_KEY', KEY)
        profile[source] = 'TUXU_CLOUD_TEST_KEY'
    configured.path.write_text(json.dumps(data))
    assert configured.list_public()['profiles'][0]['key_present']
    public = configured.update('ark', {'base_url': 'https://different.example/v1', 'api_key': ''})
    assert not public['key_present'] and not public['ready']
    changed = json.loads(configured.path.read_text())['profiles']['ark']
    assert not set(changed) & {'api_key', 'key_file', 'key_env', 'credential_origin'}
    assert not configured.test('ark')['ok']


def test_explicit_new_origin_key_and_same_origin_path_revision(configured):
    before = configured.target('ark', 'landscape')
    configured.update('ark', {'base_url': 'https://ark.cn-beijing.volces.com/another/path'})
    assert configured.list_public()['profiles'][0]['key_present']
    after = configured.target('ark', 'landscape')
    assert before['config_revision'] != after['config_revision']
    with pytest.raises(ModelError, match='配置已改变'):
        configured.ensure_target(before)
    result = configured.update('ark', {'base_url': 'https://different.example/v1', 'api_key': 'NEW_ORIGIN_KEY'})
    assert result['ready']
    assert json.loads(configured.path.read_text())['profiles']['ark']['credential_origin'] == 'https://different.example:443'


def test_server_file_and_env_need_exact_origin_binding_and_rotation_changes_revision(configured, tmp_path, monkeypatch):
    path = tmp_path / 'credential'
    path.write_text(KEY)
    data = json.loads(configured.path.read_text())
    profile = data['profiles']['ark']
    profile.pop('api_key')
    profile.pop('credential_origin')
    profile['key_file'] = str(path)
    configured.path.write_text(json.dumps(data))
    assert not configured.list_public()['profiles'][0]['ready']
    profile['credential_origin'] = credential_origin(profile['base_url'])
    configured.path.write_text(json.dumps(data))
    before = configured.target('ark', 'portrait')
    path.write_text('ROTATED_KEY')
    with pytest.raises(ModelError, match='配置已改变'):
        configured.ensure_target(before)
    profile.pop('key_file')
    profile['key_env'] = 'TUXU_CLOUD_TEST_KEY'
    monkeypatch.setenv('TUXU_CLOUD_TEST_KEY', KEY)
    configured.path.write_text(json.dumps(data))
    assert configured.list_public()['profiles'][0]['ready']


@pytest.mark.parametrize('url', ['http://example.com/v1', 'https://localhost/v1', 'https://127.0.0.1/v1',
                                'https://[::1]/v1', 'https://user:password@example.com/v1',
                                'https://example.com/v1?key=secret', 'https://example.com:8443/v1',
                                'https://example.com/#token', 'https://example.local/v1'])
def test_configuration_rejects_private_or_credential_urls(configured, url):
    with pytest.raises(ValueError):
        configured.update('ark', {'base_url': url})


def test_config_rejects_client_file_env_and_invalid_boolean(configured):
    for body in ({'key_file': '/tmp/private'}, {'key_env': 'HOME'}, {'enabled': 1}, {'clear_key': 'true'},
                 {'api_key': 'bad\nkey'}, {'api_key': KEY, 'clear_key': True}):
        with pytest.raises(ValueError):
            configured.update('ark', body)


@pytest.mark.parametrize('profile,aspect,size', [('ark', 'landscape', '2496x1664'), ('ark', 'portrait', '1664x2496'),
                                              ('ark', 'square', '2048x2048'), ('openai', 'landscape', '1536x1024'),
                                              ('codex', 'portrait', '1024x1536')])
def test_frozen_targets_ratio_model_no_credentials_and_codex_bridge(configured, profile, aspect, size):
    if profile == 'openai':
        configured.update('openai', {'api_key': KEY, 'enabled': True})
    target = configured.target(profile, aspect, 'medium')
    assert target['size'] == size and target['quality'] == 'medium'
    assert set(target) == {'id', 'provider', 'model', 'base_url', 'size', 'quality', 'config_revision'}
    assert KEY not in json.dumps(target)
    configured.ensure_target(target)
    changed = copy.deepcopy(target)
    changed['model'] = 'changed'
    with pytest.raises(ModelError):
        configured.ensure_target(changed)
    if profile == 'codex':
        assert target['base_url'] == 'codex://imagegen' and target['config_revision'] == 'codex-bridge-v1'
    if profile == 'ark':
        width, height = map(int, size.split('x'))
        assert width * height >= 3_686_400


def test_connection_test_models_only_fixed_errors_and_no_generation(configured, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'id': 'doubao-seedream-4-5-251128'}]})
    settings = mock_http(monkeypatch, respond)
    assert configured.test('ark') == {'ok': True, 'model_found': True,
        'models': [{'id': 'doubao-seedream-4-5-251128'}], 'revision': configured.target('ark', 'square')['config_revision']}
    assert len(calls) == 1 and calls[0].method == 'GET' and calls[0].url.path.endswith('/models')
    assert calls[0].headers['authorization'] == 'Bearer ' + KEY
    assert calls[0].url.host == '8.8.8.8' and calls[0].headers['host'] == 'ark.cn-beijing.volces.com'
    assert calls[0].extensions['sni_hostname'] == 'ark.cn-beijing.volces.com'
    assert settings == [{'trust_env': False, 'follow_redirects': False}]


@pytest.mark.parametrize('response', [httpx.Response(401, text=KEY + PROMPT), httpx.Response(302, headers={'location': 'https://other.example'}),
                                   httpx.Response(200, text=KEY + PROMPT)])
def test_connection_failure_never_echoes_error_body(configured, monkeypatch, response):
    mock_http(monkeypatch, lambda request: response)
    value = configured.test('ark')
    assert not value['ok'] and KEY not in json.dumps(value) and PROMPT not in json.dumps(value)


@pytest.mark.parametrize('provider', ['ark', 'openai'])
def test_real_request_shape_single_image_and_restart_cached_result(configured, monkeypatch, tmp_path, provider):
    if provider == 'openai':
        configured.update(provider, {'api_key': KEY, 'enabled': True})
    calls = []
    def respond(request):
        calls.append(request)
        return success(request)
    settings = mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    target = configured.target(provider, 'landscape')
    request_id = adapter.submit(target, {'text': PROMPT, 'negative': '', 'global_text': 'WRONG_LOCAL_BACKGROUND'}, 'one')
    body = json.loads(calls[0].content)
    assert body['prompt'] == PROMPT and body['size'] == target['size']
    assert 'WRONG_LOCAL_BACKGROUND' not in calls[0].content.decode()
    if provider == 'ark':
        assert body['stream'] is False and body['watermark'] is False and body['sequential_image_generation'] == 'disabled'
        assert body['response_format'] == 'b64_json' and 'n' not in body
    else:
        assert body['n'] == 1 and body['quality'] == 'high' and body['output_format'] == 'png' and 'response_format' not in body
    configured.update(provider, {'api_key': 'CHANGED_KEY'})
    restarted = CloudImageAdapter(configured, tmp_path / 'adapter')
    assert restarted.find_submission('one') == request_id
    assert restarted.submit(target, {'text': 'must not be resent'}, 'one') == request_id
    destination = tmp_path / 'consumer.png'
    assert restarted.result(request_id, destination) == destination
    with Image.open(destination) as image:
        assert image.size == (128, 96) and image.mode == 'RGBA' and not image.info
        assert image.getpixel((0, 0)) == (150, 40, 60, 200)
    record = json.loads(restarted._path(request_id).read_text())
    assert record['post_count'] == 1 and record['usage'] == {'total_tokens': 10, 'input_tokens': 4}
    assert KEY not in json.dumps(record) and PROMPT not in json.dumps(record)
    assert len(calls) == 1 and settings[0] == {'trust_env': False, 'follow_redirects': False}


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429])
def test_definitive_4xx_is_rejection_and_same_key_never_resubmits(configured, monkeypatch, tmp_path, status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, text=KEY + PROMPT)
    mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    for _ in range(2):
        with pytest.raises(RejectedSubmissionError) as caught:
            adapter.submit(configured.target('ark', 'square'), PROMPT, 'rejected')
        assert KEY not in str(caught.value) and PROMPT not in str(caught.value)
    assert len(calls) == 1 and adapter.find_submission('rejected') is None
    assert adapter._record(adapter._request_id('rejected'))['status'] == 'rejected'


@pytest.mark.parametrize('code', ['ModelNotOpen', 'InvalidEndpointOrModel', 'InvalidParameter', 'InvalidApiKey',
                                'AuthenticationError', 'PermissionDenied', 'ModelNotFound', 'NotFound'])
def test_known_rejection_category_only_is_cached_and_message_never_echoed(configured, monkeypatch, tmp_path, code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(404, json={'error': {'code': code, 'message': KEY + PROMPT}, 'request_id': KEY})
    mock_http(monkeypatch, respond)
    target = configured.target('ark', 'square')
    for _ in range(2):
        adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
        with pytest.raises(RejectedSubmissionError) as caught:
            adapter.submit(target, PROMPT, 'category')
        assert str(caught.value) == cloud._REJECTION_MESSAGES[code]
        assert KEY not in str(caught.value) and PROMPT not in str(caught.value)
    record = adapter._record(adapter._request_id('category'))
    assert record['error_code'] == code and record['status'] == 'rejected' and record['post_count'] == 1
    assert 'message' not in record and 'error' not in record and KEY not in json.dumps(record) and PROMPT not in json.dumps(record)
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['unknown', 'missing', 'wrong_type', 'oversized', 'invalid_json', 'read_timeout'])
def test_unrecognized_or_unreadable_rejection_diagnostics_remain_safe_rejected(configured, monkeypatch, tmp_path, kind):
    calls = []
    class TimedOutBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout(KEY + PROMPT)
            yield b''
    def respond(request):
        calls.append(request)
        if kind == 'read_timeout':
            return httpx.Response(404, stream=TimedOutBody())
        if kind == 'oversized':
            return httpx.Response(404, json={'error': {'code': 'ModelNotOpen', 'message': KEY * 1000}})
        if kind == 'invalid_json':
            return httpx.Response(404, text=KEY + PROMPT)
        value = {'error': {'code': KEY + PROMPT, 'message': KEY + PROMPT}}
        if kind == 'missing':
            value['error'].pop('code')
        elif kind == 'wrong_type':
            value['error']['code'] = {'ModelNotOpen': KEY}
        return httpx.Response(404, json=value)
    mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(RejectedSubmissionError) as caught:
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'unreadable')
    assert str(caught.value) == cloud._REJECTED
    record = adapter._record(adapter._request_id('unreadable'))
    assert record['status'] == 'rejected' and 'error_code' not in record and record['http_status'] == 404
    assert KEY not in json.dumps(record) and PROMPT not in json.dumps(record) and len(calls) == 1


@pytest.mark.parametrize('kind', ['timeout', 'disconnect', '500', 'redirect', 'json', 'base64', 'not_image', 'two_images'])
def test_unknown_results_fixed_error_and_restart_no_second_post(configured, monkeypatch, tmp_path, kind):
    calls = []
    def respond(request):
        calls.append(request)
        if kind == 'timeout':
            raise httpx.ReadTimeout(KEY + PROMPT)
        if kind == 'disconnect':
            raise httpx.ReadError(KEY + PROMPT)
        if kind == '500':
            return httpx.Response(500, text=KEY + PROMPT)
        if kind == 'redirect':
            return httpx.Response(302, headers={'location': 'https://another.example/v1'}, text=KEY)
        if kind == 'json':
            return httpx.Response(200, text=KEY)
        if kind == 'base64':
            return httpx.Response(200, json={'data': [{'b64_json': '!bad'}]})
        if kind == 'not_image':
            return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(b'not image').decode()}]})
        return httpx.Response(200, json={'data': [{}, {}]})
    mock_http(monkeypatch, respond)
    target = configured.target('ark', 'square')
    for _ in range(2):
        adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
        with pytest.raises(ModelError, match='提交结果未知') as caught:
            adapter.submit(target, PROMPT, 'unknown')
        assert type(caught.value) is ModelError and KEY not in str(caught.value) and PROMPT not in str(caught.value)
    assert len(calls) == 1 and adapter.find_submission('unknown') is None
    record = adapter._record(adapter._request_id('unknown'))
    assert record['status'] == 'unknown' and record['post_count'] == 1
    assert KEY not in json.dumps(record) and PROMPT not in json.dumps(record)


def test_durable_intent_crash_blocks_repost(configured, monkeypatch, tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        raise KeyboardInterrupt
    mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    target = configured.target('ark', 'square')
    with pytest.raises(KeyboardInterrupt):
        adapter.submit(target, PROMPT, 'crash')
    restarted = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(ModelError, match='提交结果未知'):
        restarted.submit(target, PROMPT, 'crash')
    assert len(calls) == 1 and restarted._record(restarted._request_id('crash'))['status'] == 'intent'


def test_successful_clean_image_survives_crash_before_completion_state(configured, monkeypatch, tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        return success(request)
    mock_http(monkeypatch, respond)
    actual_save = cloud._atomic_json
    def crash_on_completion(path, record):
        if record.get('status') == 'completed':
            raise KeyboardInterrupt
        actual_save(path, record)
    monkeypatch.setattr(cloud, '_atomic_json', crash_on_completion)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    target = configured.target('ark', 'square')
    with pytest.raises(KeyboardInterrupt):
        adapter.submit(target, PROMPT, 'success-crash')
    monkeypatch.setattr(cloud, '_atomic_json', actual_save)
    configured.update('ark', {'clear_key': True})
    restarted = CloudImageAdapter(configured, tmp_path / 'adapter')
    request_id = restarted.find_submission('success-crash')
    assert request_id and restarted._record(request_id)['status'] == 'completed'
    restarted.result(request_id, tmp_path / 'consumer.png')
    assert len(calls) == 1 and (tmp_path / 'consumer.png').is_file()


def test_simultaneous_adapter_instances_share_exactly_one_post(configured, monkeypatch, tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        return success(request)
    mock_http(monkeypatch, respond)
    target = configured.target('ark', 'square')
    adapters = [CloudImageAdapter(configured, tmp_path / 'adapter') for _ in range(2)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda adapter: adapter.submit(target, PROMPT, 'concurrent'), adapters))
    assert len(calls) == 1 and ids[0] == ids[1]


def test_private_dns_blocks_auth_post_before_durable_intent(configured, monkeypatch, tmp_path):
    calls = []
    mock_http(monkeypatch, lambda request: calls.append(request) or success(request))
    monkeypatch.setattr(cloud.socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('192.168.1.5', 443))])
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(ModelError, match='公开网络'):
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'private')
    assert calls == [] and adapter._record(adapter._request_id('private')) is None


def test_ark_url_download_has_no_authorization_and_does_not_persist_token(configured, monkeypatch, tmp_path):
    calls = []
    token = 'PRIVATE_DOWNLOAD_TOKEN'
    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            return httpx.Response(200, json={'data': [{'url': 'https://cdn.example/image.png?token=' + token}]})
        assert 'authorization' not in request.headers
        assert request.url.host == '8.8.8.8' and request.headers['host'] == 'cdn.example'
        assert request.extensions['sni_hostname'] == 'cdn.example'
        return httpx.Response(200, content=png())
    mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    request_id = adapter.submit(configured.target('ark', 'portrait'), PROMPT, 'url')
    assert len(calls) == 2 and adapter.find_submission('url') == request_id
    assert token not in adapter._path(request_id).read_text()


@pytest.mark.parametrize('url', ['http://cdn.example/image.png', 'https://127.0.0.1/image.png', 'https://user:password@cdn.example/image.png'])
def test_invalid_returned_url_never_downloads_and_keeps_submission_unknown(configured, monkeypatch, tmp_path, url):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'url': url}]})
    mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(ModelError, match='提交结果未知'):
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'bad-url')
    assert len(calls) == 1


def test_bounded_response_and_cache_integrity(configured, monkeypatch, tmp_path):
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={'content-length': str(cloud.MAX_JSON_BYTES + 1)}, content=b'{}'))
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(ModelError, match='提交结果未知'):
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'large')
    mock_http(monkeypatch, success)
    request_id = adapter.submit(configured.target('ark', 'square'), PROMPT, 'good')
    (adapter.directory / (request_id + '.png')).write_bytes(png())
    with pytest.raises(ModelError, match='缓存校验'):
        adapter.result(request_id, tmp_path / 'result.png')


def test_complete_chinese_cloud_scene_controls_and_normalized_bbox_preserve_source():
    director = {**default_director(), 'style_id': 'photographic', 'brightness': 'high', 'camera': 'topdown',
                'background': '纯白空白桌面', 'notes': '保留用户指定普通杯柄', 'objects': [
        {'id': 'cup', 'label': '红杯', 'description': '普通单个红色圆柱杯，只有一个正常弯曲杯柄。',
         'x': 8, 'y': 25, 'width': 36, 'height': 55, 'depth': 'midground'},
        {'id': 'book', 'label': '蓝书', 'description': '一本合上的纯蓝封面书。',
         'x': 56, 'y': 38, 'width': 36, 'height': 40, 'depth': 'midground'}]}
    slots = [{'id': 's', 'description': PROMPT, 'director': director, 'global_text': 'WRONG_GLOBAL'}]
    before = copy.deepcopy(slots)
    compiled = compose_cloud_prompts(slots, '其他风格')
    prompt = compiled['s']
    data = json.loads(prompt['text'].split('\n', 1)[1])
    assert data['template'] == 'tuxu-community-v1' and data['style'] == 'photograph'
    assert data['scene'] == PROMPT and data['brightness'] == 'bright' and data['camera'] == 'overhead view'
    assert data['background'] == director['background'] and data['notes'] == director['notes']
    assert data['objects'][0]['box_xywh'] == [0.08, 0.25, 0.36, 0.55]
    assert data['objects'][1]['box_xywh'] == [0.56, 0.38, 0.36, 0.4]
    assert 'WRONG_GLOBAL' not in prompt['text'] and slots == before
    assert prompt['regions'] == [{'object_id': 'cup', 'text': '红杯 普通单个红色圆柱杯，只有一个正常弯曲杯柄。'},
                                 {'object_id': 'book', 'text': '蓝书 一本合上的纯蓝封面书。'}]
    assert 'background_text' not in prompt and 'global_text' not in prompt and 'conditioning_mode' not in prompt


def test_cloud_prompt_inherited_style_legacy_without_objects_and_length_guard():
    compiled = compose_cloud_prompts([{'id': 's', 'description': '春天的一片树林。'}], '水彩风格')
    assert '水彩风格' in compiled['s']['text'] and 'regions' not in compiled['s']
    with pytest.raises(ValueError, match='6,000'):
        compose_cloud_prompts([{'id': 's', 'description': 'x' * 6000}], '')
    with pytest.raises(ValueError):
        compose_cloud_prompts([{'id': 's', 'description': '一'}, {'id': 's', 'description': '二'}], '')


def verification(store, provider='ark'):
    return next(profile['verification'] for profile in store.list_public()['profiles'] if profile['id'] == provider)


@pytest.mark.parametrize('provider,values,expected', [
    ('ark', ['doubao-seedream-4-5-251128', 'doubao-seedream-3-0-t2i-250415', 'ep-user-endpoint',
             'doubao-seedance-2-0', 'doubao-seedream-4-5-251128', 'doubao-seedream-BAD', 'doubao-seedream-' + 'x' * 160],
     ['doubao-seedream-3-0-t2i-250415', 'doubao-seedream-4-5-251128']),
    ('openai', ['gpt-image-2', 'gpt-image-1.5', 'gpt-5', 'dall-e-2', 'dall-e-3', 'gpt-image-2',
                'gpt-image-secret/path', 'gpt-image-X'], ['dall-e-2', 'dall-e-3', 'gpt-image-1.5', 'gpt-image-2']),
])
def test_v7_candidate_ids_only_are_finite_sorted_deduplicated(configured, monkeypatch, provider, values, expected):
    if provider == 'openai':
        configured.update(provider, {'api_key': KEY, 'enabled': True})
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={
        'data': [{'id': item, 'name': KEY, 'description': PROMPT, 'permission': KEY} for item in values], 'secret': KEY}))
    result = configured.test(provider)
    assert result['ok'] and result['models'] == [{'id': item} for item in expected]
    assert KEY not in json.dumps(result) and PROMPT not in json.dumps(result)
    assert verification(configured, provider)['connection'] == 'ok'
    assert verification(configured, provider)['generation'] == 'untested'


def test_v7_candidate_limit_and_lowercase_key_reflection(configured, monkeypatch):
    key = 'sk-lowercase-canary'
    configured.update('openai', {'api_key': key, 'enabled': True})
    values = ['gpt-image-' + str(i).zfill(3) for i in range(100)] + ['gpt-image-' + key]
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={'data': [{'id': item} for item in reversed(values)]}))
    result = configured.test('openai')
    assert len(result['models']) == 64 and result['models'][0] == {'id': 'gpt-image-000'}
    assert result['models'][-1] == {'id': 'gpt-image-063'}
    assert key not in json.dumps(result)


@pytest.mark.parametrize('data', [None, {}, 'models', [None], [{'id': 4}], [{'name': 'gpt-image-2'}]])
def test_v7_invalid_model_data_has_no_candidates_and_failed_connection(configured, monkeypatch, data):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={'data': data, 'error': {'message': KEY}}))
    result = configured.test('ark')
    assert not result['ok'] and result['models'] == [] and KEY not in json.dumps(result)
    assert verification(configured)['connection'] == 'failed'
    assert verification(configured)['model_listed'] is None


def test_v7_listed_ep_is_not_exposed_as_an_image_candidate(configured, monkeypatch):
    configured.update('ark', {'model': 'ep-approved-endpoint'})
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={'data': [{'id': 'ep-approved-endpoint'}]}))
    result = configured.test('ark')
    assert result['ok'] and result['model_found'] and result['models'] == []


def test_v7_generation_and_connection_are_independent_and_label_does_not_reset(configured, monkeypatch, tmp_path):
    before = configured.target('ark', 'square')
    mock_http(monkeypatch, success)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    adapter.submit(before, PROMPT, 'verified-success')
    assert verification(configured)['generation'] == 'succeeded'
    assert verification(configured)['connection'] == 'untested'
    assert 'generation_at' in verification(configured)
    assert configured.target('ark', 'square') == before
    configured.update('ark', {'label': '我的豆包'})
    assert configured.target('ark', 'square') == before and verification(configured)['generation'] == 'succeeded'
    mock_http(monkeypatch, lambda request: httpx.Response(401, json={'error': {'code': 'InvalidApiKey', 'message': KEY}}))
    configured.test('ark')
    value = verification(configured)
    assert value['generation'] == 'succeeded' and value['connection'] == 'failed'
    assert value['error_code'] == 'InvalidApiKey' and value['message'] in cloud.PUBLIC_CLOUD_MESSAGES
    assert configured.verification_path.stat().st_mode & 0o777 == 0o600


def test_v7_configuration_roundtrip_never_revives_success(configured):
    a = configured.target('ark', 'square')
    epoch_a = configured.capture_verification(a)
    configured.record_generation(a, 'succeeded', verification_epoch=epoch_a)
    assert verification(configured)['generation'] == 'succeeded'
    configured.update('ark', {'model': 'doubao-seedream-other'})
    assert verification(configured) == {'connection': 'untested', 'model_listed': None, 'generation': 'untested'}
    configured.update('ark', {'model': a['model']})
    assert configured.target('ark', 'square') == a
    assert configured.capture_verification(a) != epoch_a
    configured.record_generation(a, 'succeeded', verification_epoch=epoch_a)
    assert verification(configured) == {'connection': 'untested', 'model_listed': None, 'generation': 'untested'}
    assert 'verification_epoch' not in json.dumps(configured.list_public())


def test_v7_inflight_get_config_roundtrip_discards_stale_result(configured, monkeypatch):
    before = configured.target('ark', 'square')
    def respond(request):
        configured.update('ark', {'model': 'doubao-seedream-other'})
        configured.update('ark', {'model': before['model']})
        return httpx.Response(200, json={'data': [{'id': before['model']}]})
    mock_http(monkeypatch, respond)
    result = configured.test('ark')
    assert configured.target('ark', 'square') == before
    assert not result['ok'] and result['models'] == [] and result['error'] == cloud._CONFIG_CHANGED
    assert verification(configured) == {'connection': 'untested', 'model_listed': None, 'generation': 'untested'}


def test_v7_inflight_success_image_survives_but_old_epoch_cannot_verify_current_config(configured, monkeypatch, tmp_path):
    target = configured.target('ark', 'square')
    calls = []
    def respond(request):
        calls.append(request)
        configured.update('ark', {'model': 'doubao-seedream-other'})
        configured.update('ark', {'model': target['model']})
        return success(request)
    mock_http(monkeypatch, respond)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    request_id = adapter.submit(target, PROMPT, 'inflight')
    adapter.result(request_id, tmp_path / 'safe-image.png')
    assert verification(configured)['generation'] == 'untested'
    restarted = CloudImageAdapter(configured, tmp_path / 'adapter')
    assert restarted.submit(target, PROMPT, 'inflight') == request_id and len(calls) == 1
    assert verification(configured)['generation'] == 'untested'


@pytest.mark.parametrize('state', ['rejected', 'unknown'])
def test_v7_successful_get_never_erases_generation_diagnostic(configured, monkeypatch, state):
    target = configured.target('ark', 'square')
    configured.record_generation(target, state, 404 if state == 'rejected' else None,
        'ModelNotOpen' if state == 'rejected' else None, verification_epoch=configured.capture_verification(target))
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={'data': [{'id': target['model']}]}))
    result = configured.test('ark')
    value = verification(configured)
    assert result['ok'] and value['connection'] == 'ok' and value['generation'] == state
    assert value['message'] == (cloud._REJECTION_MESSAGES['ModelNotOpen'] if state == 'rejected' else cloud._UNKNOWN)
    if state == 'rejected':
        assert value['error_code'] == 'ModelNotOpen' and value['http_status'] == 404


@pytest.mark.parametrize('kind', ['success', 'rejected', 'unknown'])
def test_v7_sidecar_write_failure_never_changes_durable_submission_outcome(configured, monkeypatch, tmp_path, kind):
    calls = []
    def respond(request):
        calls.append(request)
        if kind == 'success':
            return success(request)
        if kind == 'rejected':
            return httpx.Response(404, json={'error': {'code': 'InvalidEndpointOrModel.NotFound', 'message': KEY}})
        raise httpx.ReadTimeout(KEY)
    mock_http(monkeypatch, respond)
    actual = cloud._atomic_json
    def fail_sidecar(path, value):
        if path == configured.verification_path:
            raise OSError(KEY + PROMPT)
        actual(path, value)
    monkeypatch.setattr(cloud, '_atomic_json', fail_sidecar)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    target = configured.target('ark', 'square')
    if kind == 'success':
        request_id = adapter.submit(target, PROMPT, kind)
        assert adapter.find_submission(kind) == request_id
    else:
        expected = RejectedSubmissionError if kind == 'rejected' else ModelError
        with pytest.raises(expected) as caught:
            adapter.submit(target, PROMPT, kind)
        assert type(caught.value) is expected and KEY not in str(caught.value)
    cached = adapter._record(adapter._request_id(kind))
    assert cached['status'] == {'success': 'completed', 'rejected': 'rejected', 'unknown': 'unknown'}[kind]
    assert cached['post_count'] == 1 and len(calls) == 1


def test_v7_optional_diagnostic_cache_write_failure_keeps_persisted_4xx_rejection(configured, monkeypatch, tmp_path):
    calls = []
    mock_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(404,
        json={'error': {'code': 'InvalidEndpointOrModel.NotFound', 'message': KEY}}))
    actual = cloud._atomic_json
    def fail_only_optional_code(path, value):
        if 'error_code' in value:
            raise OSError(KEY)
        actual(path, value)
    monkeypatch.setattr(cloud, '_atomic_json', fail_only_optional_code)
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(RejectedSubmissionError):
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'diagnostic-fail')
    record = adapter._record(adapter._request_id('diagnostic-fail'))
    assert record['status'] == 'rejected' and 'error_code' not in record and len(calls) == 1


def test_v7_sidecar_projection_cannot_publish_arbitrary_fields(configured):
    target = configured.target('ark', 'square')
    raw = {'schema_version': 1, 'profiles': {'ark': {'revision': target['config_revision'],
        'epoch': configured.capture_verification(target), 'api_key': KEY,
        'connection': {'status': 'failed', 'model_listed': KEY, 'message': PROMPT},
        'generation': {'status': 'rejected', 'at': PROMPT, 'http_status': True,
                       'error_code': 'InvalidEndpointOrModel.NotFound.' + KEY, 'message': PROMPT}}}}
    configured.verification_path.write_text(json.dumps(raw))
    value = verification(configured)
    assert value['connection'] == 'failed' and value['generation'] == 'rejected'
    assert value['model_listed'] is None and value['message'] == cloud._REJECTED
    assert 'error_code' not in value and 'http_status' not in value and 'generation_at' not in value
    assert KEY not in json.dumps(value) and PROMPT not in json.dumps(value)
    configured.verification_path.write_bytes(b'x' * (128 * 1024 + 1))
    assert verification(configured) == {'connection': 'untested', 'model_listed': None, 'generation': 'untested'}


def test_v7_legacy_cache_import_is_once_generic_untimed_and_never_mutates_cache(configured, tmp_path):
    data = json.loads(configured.path.read_text())
    data['profiles']['ark'].pop('verification_epoch')
    configured.path.write_text(json.dumps(data))
    target = configured.target('ark', 'square')
    directory = tmp_path / 'adapter' / 'cloud-image-cache'
    directory.mkdir(parents=True)
    request_id = CloudImageAdapter._request_id('legacy')
    cached = {'request_id': request_id, 'status': 'rejected', 'post_count': 1, 'provider': 'ark',
              'model': target['model'], 'config_revision': target['config_revision'], 'http_status': 404,
              'remote_body': KEY + PROMPT}
    cache = directory / (request_id + '.json')
    cache.write_text(json.dumps(cached))
    before = cache.read_bytes()
    configured.import_generation_cache(directory)
    value = verification(configured)
    assert value['generation'] == 'rejected' and value['http_status'] == 404 and value['message'] == cloud._REJECTED
    assert 'error_code' not in value and 'generation_at' not in value and cache.read_bytes() == before
    configured.record_generation(target, 'succeeded', verification_epoch='legacy-v1')
    configured.import_generation_cache(directory)
    assert verification(configured)['generation'] == 'succeeded' and cache.read_bytes() == before
    configured.update('ark', {'model': 'doubao-seedream-other'})
    configured.update('ark', {'model': target['model']})
    configured.import_generation_cache(directory)
    assert verification(configured)['generation'] == 'untested' and cache.read_bytes() == before


@pytest.mark.parametrize('code', ['InvalidEndpointOrModel.NotFound', 'InvalidEndpointOrModel.ModelIDAccessDisabled',
    'InvalidEndpoint.ClosedEndpoint', 'MissingParameter', 'OperationDenied.ServiceOverdue', 'NotFound.',
    'RateLimitExceeded.EndpointRPMExceeded', 'RateLimitExceeded.EndpointTPMExceeded',
    'RateLimitExceeded.EndpointFlexTPMExceeded', 'RateLimitExceeded.ModelAccountRpmRateLimitExceeded'])
def test_v7_official_dotted_codes_are_exact_finite_fixed_chinese(configured, monkeypatch, tmp_path, code):
    mock_http(monkeypatch, lambda request: httpx.Response(404, json={'error': {'code': code, 'message': KEY + PROMPT}}))
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(RejectedSubmissionError) as caught:
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'dotted')
    value = verification(configured)
    assert value['generation'] == 'rejected' and value['error_code'] == code
    assert str(caught.value) == value['message'] and value['message'] in cloud.PUBLIC_CLOUD_MESSAGES
    assert KEY not in json.dumps(value) and PROMPT not in json.dumps(value)


@pytest.mark.parametrize('code', ['InvalidEndpointOrModel.NotFound.EXTRA', 'NotFound.' + KEY,
                                 'RateLimitExceeded.EndpointRPMExceeded.more', 'InvalidParameter.CODE'])
def test_v7_unsupported_code_suffixes_are_never_truncated_or_published(configured, monkeypatch, tmp_path, code):
    mock_http(monkeypatch, lambda request: httpx.Response(404, json={'error': {'code': code, 'message': KEY + PROMPT}}))
    adapter = CloudImageAdapter(configured, tmp_path / 'adapter')
    with pytest.raises(RejectedSubmissionError) as caught:
        adapter.submit(configured.target('ark', 'square'), PROMPT, 'suffix')
    assert str(caught.value) == cloud._REJECTED and 'error_code' not in verification(configured)
