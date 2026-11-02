"""Generic adapter protocol tests: fixture images are NOT AI generation evidence."""
import base64
import concurrent.futures
import hashlib
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from backend.app.agent_adapter import AdapterError, dispatch, load_adapter, local_endpoint, png_bytes
from backend.app.local_agent import atomic_json


def request():
    prompt = 'Frozen image prompt. 一只杯子。\nNo additional objects.'
    return {'protocol': 'tuxu-local-image-v1', 'request_id': 'fixture-request', 'approval_id': 'fixture-approval',
            'prompt': prompt, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'options': {'size': '1024x1024', 'quality': 'high'}}


def image_bytes():
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo(); info.add_text('prompt', 'PRIVATE_FIXTURE_METADATA')
    Image.new('RGB', (96, 96), 'green').save(output, format='PNG', pnginfo=info)
    return output.getvalue()


def config(tmp_path, body):
    path = tmp_path / 'adapter.json'
    atomic_json(path, body)
    return path


def command_config(tmp_path, code=None):
    script = tmp_path / 'fixture_agent.py'
    script.write_text(code or '''import json,sys
from pathlib import Path
from PIL import Image,PngImagePlugin
prompt=sys.stdin.read()
assert prompt==Path(sys.argv[2]).read_text()
assert prompt not in '\\n'.join(sys.argv)
Path('observed.txt').write_text(prompt)
calls=Path(__file__).with_suffix('.calls')
calls.write_text(calls.read_text()+'x' if calls.exists() else 'x')
print('PRIVATE_FIXTURE_LOG '+prompt)
info=PngImagePlugin.PngInfo();info.add_text('prompt',prompt)
Image.new('RGB',(96,96),'green').save(sys.argv[1],pnginfo=info)
''')
    return config(tmp_path, {'kind': 'command', 'label': '我的本机助手', 'stdin': 'prompt',
        'argv': [sys.executable, str(script), '{output_file}', '{prompt_file}'], 'timeout_seconds': 2}), script


def test_command_exact_prompt_private_files_single_dispatch(tmp_path):
    path, script = command_config(tmp_path)
    work = tmp_path / 'request'; work.mkdir(mode=0o700)
    dispatch(request(), path, work)
    assert (work / 'observed.txt').read_text() == request()['prompt']
    assert (work / 'prompt.txt').stat().st_mode & 0o777 == 0o600
    receipt = json.loads((work / 'result.json').read_text())
    assert receipt == {'status': 'completed', 'request_id': 'fixture-request',
                       'prompt_sha256': request()['prompt_sha256'], 'image': 'image.png'}
    with Image.open(work / 'image.png') as image:
        assert image.size == (96, 96) and image.info == {}
    with pytest.raises((AdapterError, FileExistsError)):
        dispatch(request(), path, work)
    assert script.with_suffix('.calls').read_text() == 'x'


@pytest.mark.parametrize('endpoint', ['http://localhost:9999/x', 'https://example.com/x',
    'http://127.0.0.1/x', 'http://127.0.0.1:0/x', 'http://127.0.0.1:65536/x',
    'http://user:secret@127.0.0.1:9999/x', 'http://127.0.0.1:9999/x?key=secret',
    'http://127.0.0.1:9999/x#secret', 'http://127.0.0.2:9999/x', 'file:///tmp/x',
    'http://127.0.0.1:9999/\\x', 'http://127.0.0.1:9999/x\n'])
def test_endpoint_rejects_dns_auth_query_redirect_destinations(endpoint):
    with pytest.raises(AdapterError): local_endpoint(endpoint)


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:9999/images', 'https://[::1]:9999/images'])
def test_literal_local_endpoint(endpoint):
    assert local_endpoint(endpoint) == endpoint


@pytest.mark.parametrize('changes', [ {'argv': ['/bin/echo', '{prompt}']}, {'stdin': 'shell'},
    {'label': '/private/path'}, {'label': 'line\nline'}, {'timeout_seconds': True},
    {'api_key': 'must-not-accept'}, {'revision_files': ['relative.py']} ])
def test_invalid_command_configuration(tmp_path, changes):
    path, _ = command_config(tmp_path)
    value = json.loads(path.read_text()); value.update(changes); atomic_json(path, value)
    with pytest.raises(AdapterError): load_adapter(path)


@pytest.fixture
def local_http():
    observed = []
    behavior = {'mode': 'success'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append((self.path, body, dict(self.headers)))
            if behavior['mode'] == 'redirect':
                self.send_response(307); self.send_header('Location', '/second'); self.end_headers(); return
            result = {'status': 'completed', 'request_id': body['request_id'],
                      'prompt_sha256': body['prompt_sha256'], 'image_base64': base64.b64encode(image_bytes()).decode()}
            if behavior['mode'] == 'hash': result['prompt_sha256'] = 'wrong'
            if behavior['mode'] == 'identity': result['request_id'] = 'other'
            if behavior['mode'] == 'base64': result['image_base64'] = 'not base64!!!!'
            if behavior['mode'] == 'image': result['image_base64'] = base64.b64encode(b'not an image').decode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(result).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/image', observed, behavior
    finally: server.shutdown(); server.server_close(); thread.join()


def test_actual_loopback_http_once_ignores_proxy_and_drops_extra_fields(tmp_path, local_http, monkeypatch):
    endpoint, observed, _ = local_http
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    path = config(tmp_path, {'kind': 'http', 'endpoint': endpoint, 'label': '本机HTTP助手'})
    dispatch({**request(), 'document': 'DO_NOT_FORWARD'}, path, tmp_path)
    assert len(observed) == 1 and observed[0][1] == request()
    assert not any(key.lower() == 'authorization' for key in observed[0][2])
    assert json.loads((tmp_path / 'result.json').read_text())['status'] == 'completed'
    with pytest.raises((AdapterError, FileExistsError)): dispatch(request(), path, tmp_path)
    assert len(observed) == 1


@pytest.mark.parametrize('mode', ['redirect', 'hash', 'identity', 'base64', 'image'])
def test_bad_http_receipt_or_redirect_never_repeats(tmp_path, local_http, mode):
    endpoint, observed, behavior = local_http; behavior['mode'] = mode
    path = config(tmp_path, {'kind': 'http', 'endpoint': endpoint})
    with pytest.raises(Exception): dispatch(request(), path, tmp_path)
    assert not (tmp_path / 'result.json').exists()
    with pytest.raises(FileExistsError): dispatch(request(), path, tmp_path)
    assert len(observed) == 1


def test_concurrent_same_request_dispatches_at_most_once(tmp_path):
    path, script = command_config(tmp_path)
    def run():
        try: dispatch(request(), path, tmp_path)
        except (AdapterError, FileExistsError): pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    assert script.with_suffix('.calls').read_text() == 'x'


def test_bad_hash_rejected_before_dispatch(tmp_path):
    path, script = command_config(tmp_path)
    with pytest.raises(AdapterError): dispatch({**request(), 'prompt_sha256': 'wrong'}, path, tmp_path)
    assert not script.with_suffix('.calls').exists() and not (tmp_path / 'adapter-started.json').exists()


def test_existing_symlink_and_non_png_rejected(tmp_path):
    path, script = command_config(tmp_path)
    (tmp_path / 'image.png').symlink_to(tmp_path / 'missing')
    with pytest.raises(AdapterError): dispatch(request(), path, tmp_path)
    assert not script.with_suffix('.calls').exists()
    jpeg = io.BytesIO(); Image.new('RGB', (96, 96)).save(jpeg, format='JPEG')
    with pytest.raises(AdapterError): png_bytes(jpeg.getvalue())


def test_response_and_image_byte_limits(tmp_path, local_http, monkeypatch):
    import backend.app.agent_adapter as adapter
    endpoint, observed, _ = local_http
    path = config(tmp_path, {'kind': 'http', 'endpoint': endpoint})
    monkeypatch.setattr(adapter, 'MAX_RESPONSE_BYTES', 20)
    with pytest.raises(AdapterError): dispatch(request(), path, tmp_path)
    assert len(observed) == 1 and not (tmp_path / 'result.json').exists()
    monkeypatch.setattr(adapter, 'MAX_IMAGE_BYTES', 10)
    with pytest.raises(AdapterError): png_bytes(image_bytes())


def test_terminal_configuration_private_copy_without_executing_agent(tmp_path):
    import subprocess
    path, script = command_config(tmp_path)
    root = Path(__file__).resolve().parents[2]
    data = tmp_path / 'community-data'
    result = subprocess.run([sys.executable, str(root / 'scripts' / 'configure_local_agent.py'),
        '--adapter-config', str(path), '--data-dir', str(data)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    status = json.loads(result.stdout)
    assert status['available'] and not status['enabled'] and status['label'] == '我的本机助手'
    assert str(path) not in result.stdout and str(script) not in result.stdout
    assert (data / 'local-agent-adapter.json').stat().st_mode & 0o777 == 0o600
    assert (data / 'local-agent.json').stat().st_mode & 0o777 == 0o600
    assert not script.with_suffix('.calls').exists()
