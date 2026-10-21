"""Private cloud image configuration and durable, single-submit image adapters.

This module does not discover credentials or fall back between providers. API keys
are bound to an HTTPS origin; only the caller's approved scene description is sent.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import warnings
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .models import ModelError, RejectedSubmissionError

MAX_JSON_BYTES = 48 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
_PROFILES = {
    'ark': {'id': 'ark', 'kind': 'ark', 'label': '豆包 · Seedream',
            'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
            'model': 'doubao-seedream-4-5-251128', 'enabled': False},
    'openai': {'id': 'openai', 'kind': 'openai', 'label': 'OpenAI · GPT Image',
               'base_url': 'https://api.openai.com/v1', 'model': 'gpt-image-2', 'enabled': False},
}
_SIZES = {
    'ark': {'landscape': '2496x1664', 'square': '2048x2048', 'portrait': '1664x2496'},
    'openai': {'landscape': '1536x1024', 'square': '1024x1024', 'portrait': '1024x1536'},
}
_UNKNOWN = '云端模型提交结果未知，请恢复原作业核查，不要重复提交。'
_REJECTED = '云端模型明确拒绝生图请求，请检查配置或调整方案后重新授权。'
_CONFIG_CHANGED = '云端模型配置已改变，请重新确认并授权。'
_TEST_FAILED = '云端连接测试未通过，请检查密钥、地址与访问权限。'
_MODEL_NOT_LISTED = '已连接，但模型列表未包含所选模型。'
_REJECTION_MESSAGES = {
    'ModelNotOpen': '云端模型尚未开通，请在供应商控制台检查开通状态后重新授权。',
    'InvalidEndpointOrModel': '云端模型或接入点不可用，请核对模型标识、接入点及账户权限后重新授权。',
    'InvalidParameter': '云端生图参数被拒绝，请核对供应商要求后重新授权。',
    'InvalidApiKey': '云端密钥验证失败，请检查供应商密钥后重新授权。',
    'AuthenticationError': '云端密钥验证失败，请检查供应商密钥后重新授权。',
    'PermissionDenied': '云端模型访问权限不足，请检查供应商权限后重新授权。',
    'ModelNotFound': '云端模型或接入点不可用，请核对模型标识、接入点及账户权限后重新授权。',
    'NotFound': '云端模型或接入点不可用，请核对模型标识、接入点及账户权限后重新授权。',
    'InvalidEndpointOrModel.NotFound': '云端模型或接入点不可用，请核对模型标识、接入点及账户权限后重新授权。',
    'InvalidEndpointOrModel.ModelIDAccessDisabled': '云端模型 ID 访问未启用，请检查并使用账户已授权的 ep 接入点后重新授权。',
    'InvalidEndpoint.ClosedEndpoint': '云端接入点已关闭，请在供应商控制台检查接入点状态后重新授权。',
    'MissingParameter': '云端生图缺少必需参数，请核对供应商要求后重新授权。',
    'OperationDenied.ServiceOverdue': '云端服务账户状态异常，请在供应商控制台检查账户后重新授权。',
    'NotFound.': '云端模型或接入点不可用，请核对模型标识、接入点及账户权限后重新授权。',
    'RateLimitExceeded.EndpointRPMExceeded': '云端请求频率达到限制，请稍后重新授权。',
    'RateLimitExceeded.EndpointTPMExceeded': '云端请求频率达到限制，请稍后重新授权。',
    'RateLimitExceeded.EndpointFlexTPMExceeded': '云端请求频率达到限制，请稍后重新授权。',
    'RateLimitExceeded.ModelAccountRpmRateLimitExceeded': '云端请求频率达到限制，请稍后重新授权。',
}
PUBLIC_CLOUD_MESSAGES = frozenset({_UNKNOWN, _REJECTED, _CONFIG_CHANGED, _TEST_FAILED, _MODEL_NOT_LISTED,
                                   *_REJECTION_MESSAGES.values()})
_MODEL_PATTERNS = {
    'ark': re.compile(r'doubao-seedream-[a-z0-9-]+'),
    'openai': re.compile(r'(?:gpt-image-[a-z0-9.-]+|dall-e-2|dall-e-3)'),
}
_REVISION = re.compile(r'[0-9a-f]{64}')
_ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _safe_time(value):
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or not 2000 <= parsed.year <= 2100:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    except ValueError:
        return None


def _safe_evidence(value, phase):
    if not isinstance(value, dict):
        return {}
    allowed = ('untested', 'ok', 'failed') if phase == 'connection' else ('untested', 'succeeded', 'rejected', 'unknown')
    if value.get('status') not in allowed:
        return {}
    result = {'status': value['status']}
    if phase == 'connection':
        result['model_listed'] = value.get('model_listed') if type(value.get('model_listed')) is bool else None
    at = _safe_time(value.get('at'))
    if at:
        result['at'] = at
    if type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:
        result['http_status'] = value['http_status']
    if isinstance(value.get('error_code'), str) and value['error_code'] in _REJECTION_MESSAGES:
        result['error_code'] = value['error_code']
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.cloud-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _url_parts(value, *, allow_query=False):
    try:
        parts = urlsplit(value)
        if (not isinstance(value, str) or len(value) > 4096 or parts.scheme != 'https' or
                not parts.hostname or parts.username is not None or parts.password is not None or
                parts.fragment or (parts.query and not allow_query) or '\\' in value or
                any(ord(c) < 33 for c in value) or parts.port not in (None, 443)):
            raise ValueError
        host = parts.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if host in ('localhost', 'localhost.localdomain') or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        return parts
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('云端地址必须是无凭据、无重定向的公开 HTTPS 地址。') from None


def credential_origin(value):
    parts = _url_parts(value)
    host = parts.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    return 'https://' + ('[' + host + ']' if ':' in host else host) + ':443'


def _public_route(value, *, allow_query=False):
    parts = _url_parts(value, allow_query=allow_query)
    try:
        addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError
    except (OSError, ValueError):
        raise ModelError('云端地址无法确认是公开网络地址，请检查配置。') from None
    # Resolve once, then connect to that checked IP. Host and TLS SNI continue
    # using the configured hostname, so a second DNS answer cannot rebind auth.
    original = httpx.URL(value)
    checked = str(original.copy_with(host=addresses[0][4][0]))
    return checked, {'Host': original.netloc.decode('ascii')}, {'sni_hostname': original.host}


class CloudConfigStore:
    def __init__(self, directory):
        self.directory = _private_directory(directory)
        self.path = self.directory / 'cloud-providers.json'
        self.verification_path = self.directory / 'cloud-verification.json'
        self.lock = threading.RLock()

    def _verification_records(self):
        """A bounded allowlist projection even if the private sidecar is malformed."""
        try:
            if not self.verification_path.exists() or self.verification_path.stat().st_size > 128 * 1024:
                return {}
            value = json.loads(self.verification_path.read_text(encoding='utf-8'))
            if value.get('schema_version') != 1 or not isinstance(value.get('profiles'), dict):
                return {}
            result = {}
            for name in _PROFILES:
                record = value['profiles'].get(name)
                if not isinstance(record, dict):
                    continue
                revision, epoch = record.get('revision'), record.get('epoch')
                if (not isinstance(revision, str) or not _REVISION.fullmatch(revision) or
                        not isinstance(epoch, str) or (epoch != 'legacy-v1' and not re.fullmatch(r'[0-9a-f]{32}', epoch))):
                    continue
                result[name] = {'revision': revision, 'epoch': epoch,
                                'cache_imported': record.get('cache_imported') is True,
                                **{phase: _safe_evidence(record.get(phase), phase) for phase in ('connection', 'generation')}}
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    @staticmethod
    def _epoch(profile):
        return profile.get('verification_epoch', 'legacy-v1')

    def _verification(self, profile):
        record = self._verification_records().get(profile['id'], {})
        if record.get('revision') != self._revision(profile) or record.get('epoch') != self._epoch(profile):
            record = {}
        connection = record.get('connection', {})
        generation = record.get('generation', {})
        result = {'connection': connection.get('status', 'untested'),
                  'model_listed': connection.get('model_listed'), 'generation': generation.get('status', 'untested')}
        for phase, evidence in (('connection', connection), ('generation', generation)):
            if evidence.get('at'):
                result[phase + '_at'] = evidence['at']
        # A later GET cannot erase an actual generation rejection or uncertainty.
        phase = 'generation' if generation.get('status') in ('rejected', 'unknown') else 'connection'
        latest = generation if phase == 'generation' else connection
        result.update({key: latest[key] for key in ('http_status', 'error_code') if key in latest})
        if latest.get('error_code'):
            result['message'] = _REJECTION_MESSAGES[latest['error_code']]
        elif phase == 'connection' and connection.get('status') == 'failed':
            result['message'] = _TEST_FAILED
        elif phase == 'connection' and connection.get('status') == 'ok' and connection.get('model_listed') is False:
            result['message'] = _MODEL_NOT_LISTED
        elif generation.get('status') == 'rejected' and phase == 'generation':
            result['message'] = _REJECTED
        elif generation.get('status') == 'unknown' and phase == 'generation':
            result['message'] = _UNKNOWN
        return result

    def _record_verification(self, profile_id, revision, phase, evidence, *, epoch=None, historical=False):
        """Best effort: display-state persistence must never affect a model request."""
        try:
            if profile_id not in _PROFILES or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
                return
            clean = _safe_evidence(evidence, phase)
            if not clean or (not historical and not clean.get('at')):
                return
            with self.lock:
                descriptor = os.open(self.directory / '.verification.lock', os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    profile = self._read()['profiles'][profile_id]
                    current_epoch = self._epoch(profile)
                    if revision != self._revision(profile) or (epoch is not None and epoch != current_epoch):
                        return
                    if historical and current_epoch != 'legacy-v1':
                        return
                    records = self._verification_records()
                    record = records.get(profile_id, {})
                    if record.get('revision') != revision or record.get('epoch') != current_epoch:
                        record = {'revision': revision, 'epoch': current_epoch}
                    if historical and (record.get('cache_imported') or record.get('generation', {}).get('status') in ('succeeded', 'rejected', 'unknown')):
                        return
                    record[phase] = clean
                    if historical:
                        record['cache_imported'] = True
                    records[profile_id] = record
                    _atomic_json(self.verification_path, {'schema_version': 1, 'profiles': records})
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
        except Exception:
            return

    def capture_verification(self, target):
        """Capture private epoch before new I/O, without changing approved targets."""
        try:
            with self.lock:
                profile = self._read()['profiles'].get(target.get('id'))
                if profile and target.get('config_revision') == self._revision(profile):
                    return self._epoch(profile)
        except Exception:
            pass
        return None

    def record_generation(self, target, state, http_status=None, error_code=None, *, verification_epoch=None, historical=False):
        if not isinstance(target, dict) or target.get('id') not in _PROFILES or target.get('provider') != target.get('id'):
            return
        self._record_verification(target['id'], target.get('config_revision'), 'generation',
            {'status': state, **({} if historical else {'at': _now()}), 'http_status': http_status, 'error_code': error_code},
            epoch=verification_epoch, historical=historical)

    def import_generation_cache(self, directory):
        """Project existing current-revision evidence without rewriting legacy caches."""
        try:
            with self.lock:
                profiles = self._read()['profiles']
                records = self._verification_records()
            candidates = {}
            for path in islice(Path(directory).glob('cloud_*.json'), 256):
                if not re.fullmatch(r'cloud_[0-9a-f]{64}\.json', path.name) or path.stat().st_size > 16 * 1024:
                    continue
                try:
                    cached = json.loads(path.read_text(encoding='utf-8'))
                    profile_id = cached.get('provider')
                    profile = profiles.get(profile_id)
                    if (not profile or self._epoch(profile) != 'legacy-v1' or
                            cached.get('verification_epoch') not in (None, 'legacy-v1') or
                            (records.get(profile_id, {}).get('epoch') == 'legacy-v1' and records[profile_id].get('cache_imported')) or
                            cached.get('request_id') != path.stem or type(cached.get('post_count')) is not int or cached['post_count'] != 1 or
                            cached.get('model') != profile['model'] or cached.get('config_revision') != self._revision(profile)):
                        continue
                    state = {'completed': 'succeeded', 'rejected': 'rejected', 'unknown': 'unknown'}.get(cached.get('status'))
                    if state:
                        # mtime orders candidate evidence only; it is not a request time.
                        candidates.setdefault(profile_id, []).append((path.stat().st_mtime, cached, state))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            for profile_id, items in candidates.items():
                _, cached, state = max(items, key=lambda item: item[0])
                self.record_generation({'id': profile_id, 'provider': profile_id, 'config_revision': cached['config_revision']},
                    state, cached.get('http_status'), cached.get('error_code'), verification_epoch='legacy-v1', historical=True)
        except Exception:
            return

    def _read(self):
        if not self.path.exists():
            return {'schema_version': 1, 'profiles': copy.deepcopy(_PROFILES)}
        try:
            if self.path.stat().st_size > 64 * 1024:
                raise ValueError
            self.path.chmod(0o600)
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema_version') != 1 or not isinstance(data.get('profiles'), dict):
                raise ValueError
            profiles = {}
            for name, defaults in _PROFILES.items():
                item = data['profiles'].get(name, {})
                if not isinstance(item, dict):
                    raise ValueError
                profiles[name] = {**defaults, **item, 'id': name, 'kind': name}
                self._validate(profiles[name])
            return {'schema_version': 1, 'profiles': profiles}
        except (ValueError, TypeError, OSError):
            raise ModelError('云端私有配置无法读取，请检查本地配置文件。') from None

    @staticmethod
    def _validate(profile):
        _url_parts(profile['base_url'])
        for field, limit in (('label', 80), ('model', 160)):
            value = profile[field]
            if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError('云端名称或模型标识无效。')
        if type(profile['enabled']) is not bool:
            raise ValueError('云端启用开关必须为布尔值。')
        if ('verification_epoch' in profile and (not isinstance(profile['verification_epoch'], str) or
                                                not re.fullmatch(r'[0-9a-f]{32}', profile['verification_epoch']))):
            raise ValueError('云端私有验证状态无效。')

    @staticmethod
    def _credential(profile):
        # Files/env are server-owned initialization only, never client-supplied paths.
        if profile.get('credential_origin') != credential_origin(profile['base_url']):
            return ''
        try:
            if profile.get('api_key'):
                value = profile['api_key']
            elif profile.get('key_env'):
                value = os.environ.get(profile['key_env'], '')
            elif profile.get('key_file'):
                source = Path(profile['key_file'])
                if source.stat().st_size > 16 * 1024:
                    return ''
                value = source.read_text(encoding='utf-8')
            else:
                return ''
            if not isinstance(value, str):
                return ''
            value = value.strip()
            if len(value) > 4096 or any(ord(c) < 32 for c in value):
                return ''
            return value
        except (OSError, TypeError, ValueError):
            return ''

    def _revision(self, profile, key=None):
        return _digest({'id': profile['id'], 'kind': profile['kind'], 'base_url': profile['base_url'],
                        'model': profile['model'], 'enabled': profile['enabled'],
                        'key_digest': hashlib.sha256((self._credential(profile) if key is None else key).encode()).hexdigest()})

    def _public(self, profile):
        present = bool(self._credential(profile))
        return {**{key: profile[key] for key in ('id', 'kind', 'label', 'base_url', 'model', 'enabled')},
                'key_present': present, 'ready': bool(profile['enabled'] and present),
                'revision': self._revision(profile), 'verification': self._verification(profile)}

    def list_public(self):
        with self.lock:
            data = self._read()
            return {'profiles': [self._public(data['profiles'][name]) for name in _PROFILES],
                    'built_in': {'id': 'codex', 'label': 'GPT 内置 · Codex 协作', 'ready': True, 'mode': 'agent'}}

    def update(self, profile_id, body):
        if profile_id not in _PROFILES or not isinstance(body, dict) or set(body) - {
                'label', 'base_url', 'model', 'enabled', 'api_key', 'clear_key'}:
            raise ValueError('云端配置字段或供应商无效。')
        if 'api_key' in body and (not isinstance(body['api_key'], str) or len(body['api_key']) > 4096 or
                                  any(ord(c) < 32 for c in body['api_key'])):
            raise ValueError('云端密钥格式无效。')
        if 'clear_key' in body and type(body['clear_key']) is not bool:
            raise ValueError('清除密钥开关必须为布尔值。')
        if body.get('clear_key') and body.get('api_key', '').strip():
            raise ValueError('请单独保存或清除密钥。')
        with self.lock:
            data = self._read()
            profile = data['profiles'][profile_id]
            previous_revision = self._revision(profile)
            previous_origin = credential_origin(profile['base_url'])
            for field in ('label', 'base_url', 'model', 'enabled'):
                if field in body:
                    profile[field] = body[field].strip().rstrip('/') if field == 'base_url' and isinstance(body[field], str) else body[field]
            self._validate(profile)
            changed_origin = credential_origin(profile['base_url']) != previous_origin
            if changed_origin or body.get('clear_key') or body.get('api_key', '').strip():
                for field in ('api_key', 'key_env', 'key_file', 'credential_origin'):
                    profile.pop(field, None)
            if body.get('api_key', '').strip():
                profile['api_key'] = body['api_key'].strip()
                profile['credential_origin'] = credential_origin(profile['base_url'])
            if self._revision(profile) != previous_revision:
                # The display epoch is private and deliberately excluded from
                # authorization hashes: restoring A after B must stay untested.
                profile['verification_epoch'] = os.urandom(16).hex()
            _atomic_json(self.path, data)
            return self._public(profile)

    def target(self, profile_id, aspect, quality='high'):
        if aspect not in _SIZES['ark'] or quality not in ('medium', 'high'):
            raise ValueError('云端图片比例或质量无效。')
        if profile_id == 'codex':
            return {'id': 'codex', 'provider': 'codex', 'model': 'codex-imagegen', 'base_url': 'codex://imagegen',
                    'size': _SIZES['openai'][aspect], 'quality': quality,
                    'config_revision': 'codex-bridge-v1'}
        if profile_id not in _PROFILES:
            raise ValueError('云端供应商无效。')
        with self.lock:
            profile = self._read()['profiles'][profile_id]
            if not self._public(profile)['ready']:
                raise ModelError('云端模型尚未完成配置，请保存密钥并启用。')
            return {'id': profile_id, 'provider': profile['kind'], 'model': profile['model'],
                    'base_url': profile['base_url'], 'size': _SIZES[profile['kind']][aspect],
                    'quality': quality, 'config_revision': self._revision(profile)}

    def ensure_target(self, target):
        if not isinstance(target, dict) or set(target) != {'id', 'provider', 'model', 'base_url', 'size', 'quality', 'config_revision'}:
            raise ModelError(_CONFIG_CHANGED)
        provider = target.get('id')
        size_map = _SIZES.get('openai' if provider == 'codex' else provider, {})
        aspect = next((aspect for aspect, size in size_map.items() if target.get('size') == size), None)
        if aspect is None:
            raise ModelError(_CONFIG_CHANGED)
        try:
            current = self.target(provider, aspect, target.get('quality'))
        except (ValueError, ModelError):
            raise ModelError(_CONFIG_CHANGED) from None
        if current != target:
            raise ModelError(_CONFIG_CHANGED)

    def _connection(self, profile_id):
        profile = self._read()['profiles'].get(profile_id)
        if not profile or not self._public(profile)['ready']:
            raise ModelError('云端模型尚未完成配置，请保存密钥并启用。')
        return profile['base_url'], self._credential(profile)

    def test(self, profile_id):
        revision, epoch, status, error_code = '', None, None, None
        result = {'ok': False, 'model_found': False, 'models': [], 'revision': revision, 'error': _TEST_FAILED}
        try:
            with self.lock:
                profile = self._read()['profiles'].get(profile_id)
                if not profile:
                    raise ValueError
                key = self._credential(profile)
                revision = self._revision(profile, key=key)
                epoch = self._epoch(profile)
                result['revision'] = revision
                if not profile['enabled'] or not key:
                    raise ValueError
                base_url = profile['base_url']
            url, headers, extensions = _public_route(base_url.rstrip('/') + '/models')
            headers['Authorization'] = 'Bearer ' + key
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
                with client.stream('GET', url, headers=headers, extensions=extensions) as response:
                    status = response.status_code
                    if status != 200:
                        error_code = _rejection_code(response)
                        if error_code:
                            result['error'] = _REJECTION_MESSAGES[error_code]
                    else:
                        value = json.loads(_bounded(response, 2 * 1024 * 1024))
                        data = value.get('data') if isinstance(value, dict) else None
                        if (not isinstance(data, list) or len(data) > 4096 or
                                any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in data)):
                            raise ValueError
                        candidates = sorted({item['id'] for item in data if 0 < len(item['id']) <= 160 and
                            _MODEL_PATTERNS[profile_id].fullmatch(item['id']) and key not in item['id']})[:64]
                        found = any(item['id'] == profile['model'] and key not in item['id'] for item in data)
                        result = {'ok': True, 'model_found': found, 'models': [{'id': item} for item in candidates],
                                  'revision': revision, **({} if found else {'error': _MODEL_NOT_LISTED})}
        except Exception:
            pass
        if revision:
            try:
                with self.lock:
                    current = self._read()['profiles'][profile_id]
                    if self._revision(current) != revision or self._epoch(current) != epoch:
                        result = {'ok': False, 'model_found': False, 'models': [], 'revision': revision, 'error': _CONFIG_CHANGED}
            except Exception:
                result = {'ok': False, 'model_found': False, 'models': [], 'revision': revision, 'error': _TEST_FAILED}
        self._record_verification(profile_id, revision, 'connection',
            {'status': 'ok' if result['ok'] else 'failed', 'model_listed': result['model_found'] if result['ok'] else None,
             'at': _now(), 'http_status': status, 'error_code': error_code}, epoch=epoch)
        # _record_verification uses the captured epoch, never the current one.
        return result


def _bounded(response, limit):
    try:
        length = response.headers.get('content-length')
        if length is not None and int(length) > limit:
            raise ModelError('云端响应超过允许大小，请检查原作业。')
        content, size = [], 0
        for piece in response.iter_bytes():
            size += len(piece)
            if size > limit:
                raise ModelError('云端响应超过允许大小，请检查原作业。')
            content.append(piece)
        return b''.join(content)
    except (ValueError, TypeError):
        raise ModelError('云端响应格式无效，请检查原作业。') from None


def _rejection_code(response):
    """Only a finite provider error category survives; all message text is ignored."""
    try:
        value = json.loads(_bounded(response, 16 * 1024))
        error = value.get('error')
        code = error.get('code') if isinstance(error, dict) else None
        return code if isinstance(code, str) and code in _REJECTION_MESSAGES else None
    except Exception:
        # The HTTP 4xx was already observed: diagnostic parsing failure cannot
        # change rejection into an ambiguous acceptance or cause another POST.
        return None


def _clean_png(content, destination):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ModelError('云端图片无法安全读取，请检查原作业。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if (image.format not in ('PNG', 'JPEG', 'WEBP') or min(image.size) < 64 or
                        image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, 'n_frames', 1) != 1):
                    raise ValueError
                image.load()
                clean = Image.new('RGBA' if image.mode in ('RGBA', 'LA') or 'transparency' in image.info else 'RGB', image.size)
                clean.paste(image.convert(clean.mode))
                destination = Path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix='.cloud-image-', dir=destination.parent)
                try:
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, 'wb') as handle:
                        clean.save(handle, format='PNG')
                        handle.flush()
                        os.fsync(handle.fileno())
                    if os.path.getsize(temporary) > MAX_IMAGE_BYTES:
                        raise ValueError
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ModelError('云端图片无法安全读取，请检查原作业。') from None


class CloudImageAdapter:
    def __init__(self, config_store, directory):
        self.config_store = config_store
        self.directory = _private_directory(Path(directory) / 'cloud-image-cache')
        self.lock = threading.RLock()
        self.config_store.import_generation_cache(self.directory)

    def _verification_result(self, record):
        try:
            state = {'completed': 'succeeded', 'rejected': 'rejected', 'unknown': 'unknown'}.get(record.get('status'))
            if not state:
                return
            epoch = record.get('verification_epoch')
            self.config_store.record_generation(
                {'id': record['provider'], 'provider': record['provider'], 'config_revision': record['config_revision']},
                state, record.get('http_status'), record.get('error_code'),
                verification_epoch=epoch or 'legacy-v1', historical=epoch is None)
        except Exception:
            return

    @staticmethod
    def _request_id(submission_key):
        if not isinstance(submission_key, str) or not submission_key or len(submission_key) > 500:
            raise RejectedSubmissionError('云端提交标识无效，尚未发送生图请求。')
        return 'cloud_' + hashlib.sha256(submission_key.encode()).hexdigest()

    def _path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r'cloud_[0-9a-f]{64}', request_id):
            raise ModelError('云端请求标识无效，请检查原作业。')
        return self.directory / (request_id + '.json')

    def _record(self, request_id):
        path = self._path(request_id)
        if not path.exists():
            return None
        try:
            if path.stat().st_size > 16 * 1024:
                raise ValueError
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('request_id') != request_id or record.get('status') not in ('intent', 'unknown', 'rejected', 'completed'):
                raise ValueError
            return record
        except (ValueError, TypeError, OSError):
            raise ModelError(_UNKNOWN) from None

    def _recover_completed_image(self, request_id, record):
        """Recover a crash between atomic clean-image save and completed state save."""
        source = self.directory / (request_id + '.png')
        if not record or record['status'] not in ('intent', 'unknown') or not source.is_file():
            return record
        try:
            if source.stat().st_size > MAX_IMAGE_BYTES:
                return record
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    if (image.format != 'PNG' or image.info or min(image.size) < 64 or
                            image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, 'n_frames', 1) != 1):
                        return record
                    image.load()
            record.update(status='completed', sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            _atomic_json(self._path(request_id), record)
            self._verification_result(record)
            return record
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return record

    @contextlib.contextmanager
    def _submission_lock(self):
        # Cross-process protection covers an app restart overlapping its old worker.
        with self.lock:
            descriptor = os.open(self.directory / '.submit.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def submit(self, target, prompt, submission_key):
        request_id = self._request_id(submission_key)
        with self._submission_lock():
            old = self._recover_completed_image(request_id, self._record(request_id))
            if old:
                if old['status'] == 'completed':
                    return request_id
                if old['status'] == 'rejected':
                    raise RejectedSubmissionError(_REJECTION_MESSAGES.get(old.get('error_code'), _REJECTED))
                raise ModelError(_UNKNOWN)
            self.config_store.ensure_target(target)
            if target['provider'] not in ('ark', 'openai'):
                raise RejectedSubmissionError('GPT 内置生图需要 Codex 协作，尚未发送云端 API 请求。')
            text = prompt.get('text') if isinstance(prompt, dict) else prompt
            if not isinstance(text, str) or not text.strip() or len(text) > 6000:
                raise RejectedSubmissionError('云端画面描述无效，尚未发送生图请求。')
            with self.config_store.lock:
                self.config_store.ensure_target(target)
                base_url, key = self.config_store._connection(target['id'])
                verification_epoch = self.config_store.capture_verification(target)
            url, headers, extensions = _public_route(base_url.rstrip('/') + '/images/generations')
            headers['Authorization'] = 'Bearer ' + key
            body = {'model': target['model'], 'prompt': text, 'size': target['size']}
            if target['provider'] == 'ark':
                body.update(response_format='b64_json', watermark=False, stream=False, sequential_image_generation='disabled')
            else:
                body.update(n=1, quality=target['quality'], output_format='png')
            record = {'request_id': request_id, 'status': 'intent', 'post_count': 1,
                      'provider': target['provider'], 'model': target['model'], 'size': target['size'],
                      'config_revision': target['config_revision'], 'verification_epoch': verification_epoch}
            _atomic_json(self._path(request_id), record)
            try:
                with httpx.Client(timeout=httpx.Timeout(360, connect=15), trust_env=False, follow_redirects=False) as client:
                    with client.stream('POST', url, json=body, headers=headers, extensions=extensions) as response:
                        if 400 <= response.status_code < 500:
                            record.update(status='rejected', http_status=response.status_code)
                            _atomic_json(self._path(request_id), record)
                            error_code = _rejection_code(response)
                            if error_code:
                                record['error_code'] = error_code
                                try:
                                    _atomic_json(self._path(request_id), record)
                                except Exception:
                                    pass
                            self._verification_result(record)
                            raise RejectedSubmissionError(_REJECTION_MESSAGES.get(error_code, _REJECTED))
                        if response.status_code != 200:
                            raise ModelError(_UNKNOWN)
                        value = json.loads(_bounded(response, MAX_JSON_BYTES))
                    data = value.get('data')
                    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                        raise ModelError(_UNKNOWN)
                    item = data[0]
                    if isinstance(item.get('b64_json'), str):
                        encoded = item['b64_json']
                        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                            raise ModelError(_UNKNOWN)
                        content = base64.b64decode(encoded, validate=True)
                    elif target['provider'] == 'ark' and isinstance(item.get('url'), str):
                        download_url, download_headers, download_extensions = _public_route(item['url'], allow_query=True)
                        with client.stream('GET', download_url, headers=download_headers, extensions=download_extensions) as download:
                            if download.status_code != 200:
                                raise ModelError(_UNKNOWN)
                            content = _bounded(download, MAX_IMAGE_BYTES)
                    else:
                        raise ModelError(_UNKNOWN)
                _clean_png(content, self.directory / (request_id + '.png'))
                usage = value.get('usage')
                if isinstance(usage, dict):
                    record['usage'] = {name: usage[name] for name in ('input_tokens', 'output_tokens', 'total_tokens', 'generated_images')
                                       if type(usage.get(name)) is int and usage[name] >= 0}
                record.update(status='completed', sha256=hashlib.sha256((self.directory / (request_id + '.png')).read_bytes()).hexdigest())
                _atomic_json(self._path(request_id), record)
                self._verification_result(record)
                return request_id
            except RejectedSubmissionError:
                raise
            except Exception:
                record['status'] = 'unknown'
                _atomic_json(self._path(request_id), record)
                self._verification_result(record)
                raise ModelError(_UNKNOWN) from None

    def find_submission(self, submission_key):
        request_id = self._request_id(submission_key)
        with self._submission_lock():
            record = self._recover_completed_image(request_id, self._record(request_id))
            return request_id if record and record['status'] == 'completed' else None

    def result(self, request_id, destination):
        with self._submission_lock():
            record = self._recover_completed_image(request_id, self._record(request_id))
        source = self.directory / (request_id + '.png')
        if not record or record['status'] != 'completed' or not source.is_file() or source.stat().st_size > MAX_IMAGE_BYTES:
            raise ModelError(_UNKNOWN)
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != record.get('sha256'):
            raise ModelError('云端图片缓存校验未通过，请检查原作业。')
        _clean_png(content, Path(destination))
        return Path(destination)


def compose_cloud_prompts(slots, style):
    """Use the public Community template; this is not a private prompt service."""
    from .community_prompts import basic_cloud_prompts
    return basic_cloud_prompts(slots, style)
