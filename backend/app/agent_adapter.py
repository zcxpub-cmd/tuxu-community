"""Owner-configured local image adapters; configuration checks never execute agents."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import string
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 16384
FIELDS = {'prompt_file', 'request_file', 'output_file', 'work_dir'}


class AdapterError(ValueError):
    """Only fixed error codes may leave the private adapter process."""


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def local_endpoint(value):
    if not isinstance(value, str) or len(value) > 512:
        raise AdapterError('invalid_local_endpoint')
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or url.hostname not in ('127.0.0.1', '::1') or
                not url.port or url.username is not None or url.password is not None or
                url.query or url.fragment or '\\' in value or any(c.isspace() for c in value)):
            raise AdapterError('invalid_local_endpoint')
    except ValueError as exc:
        raise AdapterError('invalid_local_endpoint') from exc
    return value


def load_adapter(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise AdapterError('invalid_adapter_config')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise AdapterError('invalid_adapter_config') from exc
    if not isinstance(value, dict) or value.get('kind') not in ('command', 'http'):
        raise AdapterError('invalid_adapter_kind')
    allowed = {'kind', 'label', 'timeout_seconds', 'revision_files'} | (
        {'argv', 'stdin'} if value['kind'] == 'command' else {'endpoint'})
    if set(value) - allowed:
        raise AdapterError('unknown_adapter_fields')
    label = value.get('label', '通用本机智能体')
    if not isinstance(label, str) or not re.fullmatch(r'[\w .()（）·+\-]{1,60}', label):
        raise AdapterError('invalid_adapter_label')
    timeout = value.get('timeout_seconds', 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 1 <= timeout <= 1800:
        raise AdapterError('invalid_adapter_timeout')
    dependencies = value.get('revision_files', [])
    if not isinstance(dependencies, list) or len(dependencies) > 24:
        raise AdapterError('invalid_revision_files')
    dependencies = list(dependencies)
    if value['kind'] == 'command':
        argv = value.get('argv')
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or
                any(not isinstance(arg, str) or not arg or len(arg) > 4096 or '\0' in arg for arg in argv)):
            raise AdapterError('invalid_command')
        program = Path(argv[0])
        if not program.is_absolute() or not program.is_file() or not os.access(program, os.X_OK):
            raise AdapterError('invalid_command')
        if value.get('stdin', 'request_json') not in ('request_json', 'prompt', 'none'):
            raise AdapterError('invalid_stdin')
        for arg in argv:
            try:
                for _, field, spec, conversion in string.Formatter().parse(arg):
                    if field is not None and (field not in FIELDS or spec or conversion):
                        raise AdapterError('invalid_placeholder')
            except ValueError as exc:
                raise AdapterError('invalid_placeholder') from exc
            if Path(arg).is_absolute() and Path(arg).is_file():
                dependencies.append(arg)
    else:
        local_endpoint(value.get('endpoint'))
    for entry in dependencies:
        if not isinstance(entry, str) or not Path(entry).is_absolute() or not Path(entry).is_file():
            raise AdapterError('invalid_revision_file')
    return {**value, 'label': label, 'timeout_seconds': timeout}, sorted(set(dependencies))


def adapter_bundle(path, executable):
    """Snapshot semantic configuration and full declared program content hashes."""
    config, dependencies = load_adapter(path)
    dependencies += [str(Path(__file__)), str(Path(__file__).resolve().parents[2] / 'scripts' / 'local_image_runner.py'), executable]
    files = sorted({str(Path(item).resolve()) for item in dependencies})
    return {'config': config, 'files': [{'path': item, 'sha256': file_digest(item)} for item in files]}


def private_write(path, content, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    # Refuse links even in this owner-only working directory.
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    Path(path).chmod(0o600)


def validate_request(value):
    if not isinstance(value, dict) or value.get('protocol') != 'tuxu-local-image-v1':
        raise AdapterError('invalid_request')
    for field in ('request_id', 'approval_id'):
        if not isinstance(value.get(field), str) or not 1 <= len(value[field]) <= 256:
            raise AdapterError('invalid_identity')
    prompt = value.get('prompt')
    if not isinstance(prompt, str) or not 1 <= len(prompt.encode('utf-8')) <= 262144:
        raise AdapterError('invalid_prompt')
    if hashlib.sha256(prompt.encode('utf-8')).hexdigest() != value.get('prompt_sha256'):
        raise AdapterError('invalid_prompt_hash')
    if not isinstance(value.get('options'), dict) or set(value['options']) - {'size', 'quality'}:
        raise AdapterError('invalid_options')
    # Do not forward unrelated fields even if a caller provided them.
    return {key: value[key] for key in ('protocol', 'request_id', 'approval_id', 'prompt', 'prompt_sha256', 'options')}


def png_bytes(content):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise AdapterError('invalid_image')
    with Image.open(io.BytesIO(content)) as image:
        if (image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1 or
                min(image.size) < 64 or image.width * image.height > 32_000_000):
            raise AdapterError('invalid_image')
        image.load()
        clean = Image.new('RGBA' if 'A' in image.getbands() else 'RGB', image.size)
        clean.paste(image.convert(clean.mode))
        output = io.BytesIO()
        clean.save(output, format='PNG')
    if output.tell() > MAX_IMAGE_BYTES:
        raise AdapterError('invalid_image')
    return output.getvalue()


def dispatch(request, config_path, work, expected_bundle=None, executable=None):
    request = validate_request(request)
    if expected_bundle is not None:
        if not executable:
            raise AdapterError('approved_adapter_changed')
        actual = adapter_bundle(config_path, executable)
        if actual != expected_bundle:
            raise AdapterError('approved_adapter_changed')
        config = actual['config']
    else:
        config, _ = load_adapter(config_path)
    work = Path(work).resolve()
    image = work / 'image.png'
    if image.exists() or image.is_symlink() or (work / 'result.json').exists() or (work / 'result.json').is_symlink():
        raise AdapterError('existing_output')
    marker = json.dumps({'request_id': request['request_id'], 'prompt_sha256': request['prompt_sha256']}).encode()
    private_write(work / 'adapter-started.json', marker, exclusive=True)
    # Make creation durable before dispatching anything with possible side effects.
    if os.name == 'posix':
        descriptor = os.open(work, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    raw = json.dumps(request, ensure_ascii=False).encode('utf-8')
    private_write(work / 'adapter-request.json', raw, exclusive=True)
    private_write(work / 'prompt.txt', request['prompt'].encode('utf-8'), exclusive=True)
    if config['kind'] == 'command':
        replacements = {'prompt_file': str(work / 'prompt.txt'), 'request_file': str(work / 'adapter-request.json'),
                        'output_file': str(image), 'work_dir': str(work)}
        argv = [arg.format_map(replacements) for arg in config['argv']]
        mode = config.get('stdin', 'request_json')
        data = raw if mode == 'request_json' else request['prompt'].encode('utf-8') if mode == 'prompt' else b''
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('CODEX_') or key == 'CODEX_HOME'}
        # The supervising bridge owns the timeout and process group, including descendants.
        result = subprocess.run(argv, input=data, cwd=work, shell=False, env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise AdapterError('command_failed_unknown')
        if image.is_symlink() or not image.is_file() or image.stat().st_size > MAX_IMAGE_BYTES:
            raise AdapterError('invalid_image')
        content = image.read_bytes()
    else:
        with httpx.Client(timeout=config['timeout_seconds'], trust_env=False, follow_redirects=False,
                          transport=httpx.HTTPTransport(retries=0, trust_env=False)) as client:
            with client.stream('POST', config['endpoint'], json=request) as response:
                if response.status_code != 200:
                    raise AdapterError('http_failed_unknown')
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise AdapterError('response_too_large')
        value = json.loads(body)
        if (not isinstance(value, dict) or value.get('status') != 'completed' or
                value.get('request_id') != request['request_id'] or
                value.get('prompt_sha256') != request['prompt_sha256'] or
                not isinstance(value.get('image_base64'), str)):
            raise AdapterError('invalid_http_receipt')
        content = base64.b64decode(value['image_base64'], validate=True)
    clean = png_bytes(content)
    private_write(work / 'adapter-image.tmp', clean, exclusive=True)
    (work / 'adapter-image.tmp').replace(image)
    receipt = {'status': 'completed', 'request_id': request['request_id'],
               'prompt_sha256': request['prompt_sha256'], 'image': 'image.png'}
    private_write(work / 'adapter-result.tmp', json.dumps(receipt).encode(), exclusive=True)
    (work / 'adapter-result.tmp').replace(work / 'result.json')
