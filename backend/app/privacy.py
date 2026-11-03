"""Public projections and immutable metadata-free image/download copies.

This protects the application's responses, not files or other local model servers
from a person who owns the machine.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import os
import posixpath
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from PIL import Image
from .cloud import PUBLIC_CLOUD_MESSAGES
from .one_click import MESSAGES as AUTOMATION_MESSAGES, STAGES as AUTOMATION_STAGES, STATUSES as AUTOMATION_STATUSES
from .builtin_agent import public_review

_PUBLIC_ERROR_MESSAGES = PUBLIC_CLOUD_MESSAGES | frozenset(AUTOMATION_MESSAGES.values()) | frozenset({
    '本机智能体配置已改变，请重新确认当前方案。',
    '本机智能体未能启动图片工具，未生成图片，请检查配置后重新确认。',
    '本机智能体已返回图片，但图片校验未通过。请检查画幅或改用其他生图服务；已生成请求不会自动重发。',
    '模型提交状态待核查，请恢复原作业，不要另建请求。',
    '本地模型的区域控制不可用，请检查节点或删除区域后重新保存。',
    '本地模型未接受请求，请检查模型节点或修改方案。',
    '画面描述转换未完成，请重试或调整简单描述。',
    '全文阅读覆盖检查未通过，请重新分析。',
    '模型请求等待超时，请恢复原作业核查。',
    '执行未完成，请检查本地服务记录。',
    '云端模型配置已改变，请重新确认并授权。',
    '云端模型明确拒绝生图请求，请检查配置或调整方案后重新授权。',
    '云端模型尚未开通，请在供应商控制台检查开通状态后重新授权。',
    '云端模型或接入点不可用，请核对供应商配置后重新授权。',
    '云端生图参数被拒绝，请核对供应商要求后重新授权。',
    '云端密钥验证失败，请检查供应商密钥后重新授权。',
    '云端模型访问权限不足，请检查供应商权限后重新授权。',
})


def public_error(value):
    if not value:
        return None
    value = str(value)
    if value in _PUBLIC_ERROR_MESSAGES:
        return value
    if '提交结果未知' in value or '恢复时提交结果未知' in value:
        return '模型提交状态待核查，请恢复原作业，不要另建请求。'
    if '区域控制节点' in value:
        return '本地模型的区域控制不可用，请检查节点或删除区域后重新保存。'
    if '校验拒绝' in value or '尚未发送生图' in value:
        return '本地模型未接受请求，请检查模型节点或修改方案。'
    if '提示词' in value or '英文' in value:
        return '画面描述转换未完成，请重试或调整简单描述。'
    if '全文覆盖' in value or '遗漏' in value:
        return '全文阅读覆盖检查未通过，请重新分析。'
    if '超时' in value:
        return '模型请求等待超时，请恢复原作业核查。'
    return '执行未完成，请检查本地服务记录。'


def _drop_global_text(value):
    """Private conditioning fields can occur in nested snapshots; never expose them."""
    if isinstance(value, dict):
        return {key: _drop_global_text(item) for key, item in value.items()
                if key not in ('global_text', 'conditioning_mode', 'prompt_failure_evidence',
                               '_builtin_agent', '_builtin_visual_review', '_agent_activity',
                               '_prompt_preparation_failure')}
    if isinstance(value, list):
        return [_drop_global_text(item) for item in value]
    return value


def public_task(task):
    result = copy.deepcopy(task)
    for asset in result.get('assets', []):
        # Never trust stored public-looking data or free-form model messages.
        asset.pop('visual_review', None)
        review = public_review(asset.get('_builtin_visual_review'))
        if review is not None:
            asset['visual_review'] = review
    result = _drop_global_text(result)
    automation = result.pop('automation', None)
    if isinstance(automation, dict):
        private_actions = automation.get('_actions')
        approval_action = private_actions.get('approval') if isinstance(private_actions, dict) else None
        private_consent = automation.get('_consent')
        legacy_protocol = (private_consent is None or isinstance(private_consent, dict) and
                           'preparation_failure_protocol' not in private_consent)
        identifier = automation.get('id')
        safe = {'id': identifier if isinstance(identifier, str) and re.fullmatch(r'[a-f0-9-]{36}', identifier) else '',
                'status': automation.get('status') if automation.get('status') in AUTOMATION_STATUSES else 'paused',
                'stage': automation.get('stage') if automation.get('stage') in AUTOMATION_STAGES else 'analysis',
                'message': automation.get('message') if automation.get('message') in AUTOMATION_MESSAGES.values() else AUTOMATION_MESSAGES['unknown'],
                'format': automation.get('format') if automation.get('format') in ('docx', 'markdown', 'html') else 'docx'}
        for key in ('max_images', 'completed_images', 'total_images'):
            value = automation.get(key)
            safe[key] = min(5, max(0, value)) if type(value) is int else 0
        url = automation.get('export_url')
        if isinstance(url, str) and re.fullmatch(r'/api/exports/[a-f0-9-]{36}/[a-f0-9-]{36}\.(?:docx|zip|html)', url):
            safe['export_url'] = url
        # Old preparation failures used the generic unknown message. Correct the
        # explanation without replaying or rewriting the preserved user task.
        if (automation.get('status') == 'paused' and automation.get('stage') == 'approval' and
                isinstance(approval_action, dict) and approval_action.get('state') == 'intent' and
                legacy_protocol and '_prompt_preparation_failure' not in task and
                not task.get('_prompting') and not task.get('approval') and not task.get('assets') and
                not task.get('exports') and task.get('plan') and
                all(job.get('kind') == 'analysis' for job in task.get('jobs', [])) and
                task.get('error') == '独立画面提示词未得到有效英文，请重新转换。'):
            safe['message'] = AUTOMATION_MESSAGES['prompt_failed']
        result['automation'] = safe
    for key in ('source_path', 'edited_prompt_ids', 'cancel_requested', '_prompting', 'prompts'):
        result.pop(key, None)
    result['prompt_summaries'] = {
        slot['id']: {'description': slot.get('description', '')[:240],
                     'region_count': len(slot.get('director', {}).get('objects', []))}
        for slot in (result.get('plan') or {}).get('slots', [])
    }
    result['error'] = public_error(result.get('error'))
    if result.get('approval'):
        result['approval'].pop('prompt_evidence', None)
    for job in result.get('jobs', []):
        job.pop('automation_id', None)
        job.pop('prompts', None)
        if job.get('error') or job.get('status') == 'failed':
            job['error'] = public_error(job.get('error') or job.get('message'))
            job['message'] = job['error']
        for submission in job.get('submissions', []):
            if submission.get('rejection'):
                submission['rejection'] = public_error(submission['rejection'])
    for asset in result.get('assets', []):
        for key in ('prompt', 'negative_prompt', 'scene_prompt', 'background_text', 'global_text', 'conditioning_mode', 'regions', 'sha256', 'processing_versions'):
            asset.pop(key, None)
        if asset.get('processing'):
            asset['processing'].pop('sha256', None)
        for area in asset.get('region_areas', []):
            area.pop('text', None)
        asset['image_metadata_removed'] = True
    return result


def clean_png(data):
    """Decode pixels and re-encode without text, EXIF, or a workflow sidecar."""
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        if source.format != 'PNG':
            return data
        # Materialize palette transparency before discarding metadata.
        image = source.convert('RGBA') if source.mode == 'P' or 'transparency' in source.info else source.copy()
    image.info.clear()
    output = io.BytesIO()
    image.save(output, format='PNG', pnginfo=None, exif=b'')
    return output.getvalue()


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.stem + '.', suffix='.tmp', dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sanitize_new_image(path):
    """Only call before publishing a newly generated asset; never rewrite a legacy asset."""
    path = Path(path)
    _atomic_write(path, clean_png(path.read_bytes()))


def private_image_copy(path, cache_dir, expected_sha256=None):
    path, cache_dir = Path(path), Path(cache_dir)
    source = path.read_bytes()
    identity = hashlib.sha256(source).hexdigest()
    if expected_sha256 is not None and identity != expected_sha256:
        raise ValueError('原图哈希已改变。')
    target = cache_dir / ('image-v2-' + identity + '.png')
    if expected_sha256 is not None or not target.is_file():
        _atomic_write(target, clean_png(source))
    return target


_DATA_IMAGE = re.compile(r'data:image/png;base64,([A-Za-z0-9+/=]+)')
_FIGURE = re.compile(r'<figure>.*?</figure>', re.DOTALL)


class UnsafeLegacyExport(ValueError):
    """An old export cannot be cleaned without changing an original media part."""


def _docx_image_uses(archive):
    rel_ns = '{http://schemas.openxmlformats.org/package/2006/relationships}'
    drawing_ns = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    office_ns = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    try:
        relationships = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
        document = ET.fromstring(archive.read('word/document.xml'))
    except (KeyError, ET.ParseError):
        raise UnsafeLegacyExport('旧文件需要重新导出。')
    targets = {item.attrib.get('Id'): posixpath.normpath('word/' + item.attrib.get('Target', ''))
               for item in relationships if item.tag == rel_ns + 'Relationship' and
               item.attrib.get('Type', '').endswith('/image') and item.attrib.get('TargetMode') != 'External'}
    return Counter(targets[image.attrib[office_ns + 'embed']]
                   for image in document.iter(drawing_ns + 'blip')
                   if image.attrib.get(office_ns + 'embed') in targets)


def generated_image_hashes(task):
    hashes = set()
    for asset in task.get('assets', []):
        if asset.get('source') != 'comfyui':
            continue
        if asset.get('sha256'):
            hashes.add(asset['sha256'])
        if (asset.get('processing') or {}).get('sha256'):
            hashes.add(asset['processing']['sha256'])
        for version in asset.get('processing_versions', {}).values():
            if (version.get('processing') or {}).get('sha256'):
                hashes.add(version['processing']['sha256'])
    return hashes


def clean_export(data, suffix, generated_hashes, source_docx=None):
    def is_generated(content):
        return hashlib.sha256(content).hexdigest() in generated_hashes
    if suffix in ('.docx', '.zip'):
        source, target = io.BytesIO(data), io.BytesIO()
        protected, source_uses = {}, Counter()
        if suffix == '.docx' and source_docx is not None:
            with zipfile.ZipFile(io.BytesIO(source_docx)) as original:
                protected = {name: original.read(name) for name in original.namelist() if name.startswith('word/media/')}
                source_uses = _docx_image_uses(original)
        with zipfile.ZipFile(source) as archive, zipfile.ZipFile(target, 'w') as cleaned:
            output_uses = _docx_image_uses(archive) if suffix == '.docx' and source_docx is not None else Counter()
            for info in archive.infolist():
                content = archive.read(info.filename)
                if info.filename in protected:
                    if content != protected[info.filename]:
                        raise UnsafeLegacyExport('旧文件需要重新导出。')
                    if is_generated(content) and output_uses[info.filename] > source_uses[info.filename] and clean_png(content) != content:
                        # python-docx may deduplicate generated/source drawings into one media part.
                        raise UnsafeLegacyExport('旧文件需要重新导出。')
                elif content.startswith(b'\x89PNG\r\n\x1a\n') and is_generated(content):
                    if suffix == '.zip':
                        if re.fullmatch(r'images/source_\d{4}_\d{2}\.png', info.filename):
                            pass
                        elif re.fullmatch(r'images/figure_\d{3}\.png', info.filename):
                            content = clean_png(content)
                        else:
                            raise UnsafeLegacyExport('旧文件需要重新导出。')
                    elif info.filename.startswith('word/media/'):
                        content = clean_png(content)
                    else:
                        raise UnsafeLegacyExport('旧文件需要重新导出。')
                cleaned.writestr(info, content)
        return target.getvalue()
    if suffix == '.html':
        text = data.decode('utf-8')
        figures = list(_FIGURE.finditer(text))
        for image_match in _DATA_IMAGE.finditer(text):
            image = base64.b64decode(image_match.group(1), validate=True)
            if not is_generated(image):
                continue
            figure = next((item for item in figures if item.start() <= image_match.start() < item.end()), None)
            if figure is None:
                raise UnsafeLegacyExport('旧文件需要重新导出。')
            fragment = figure.group(0)
            generated_role = re.fullmatch(r'<figure><img src="data:image/png;base64,[A-Za-z0-9+/=]+" alt="[^"]*"><figcaption>.*?</figcaption>(?:<p class="ai-disclosure">本示意图由AI生成</p>)?</figure>', fragment, re.DOTALL)
            source_role = re.fullmatch(r'<figure><img src="data:image/png;base64,[A-Za-z0-9+/=]+" alt="原稿图片"></figure>', fragment)
            if not generated_role and not source_role:
                raise UnsafeLegacyExport('旧文件需要重新导出。')
        def replace_figure(match):
            fragment = match.group(0)
            if '<figcaption>' not in fragment:
                return fragment
            def replace_image(image_match):
                image = base64.b64decode(image_match.group(1), validate=True)
                if not is_generated(image):
                    return image_match.group(0)
                return 'data:image/png;base64,' + base64.b64encode(clean_png(image)).decode('ascii')
            return _DATA_IMAGE.sub(replace_image, fragment)
        return _FIGURE.sub(replace_figure, text).encode('utf-8')
    raise ValueError('不支持的导出文件类型。')


def private_export_copy(path, cache_dir, generated_hashes, source_docx=None):
    path, cache_dir = Path(path), Path(cache_dir)
    data = path.read_bytes()
    identity = hashlib.sha256(data + '\n'.join(sorted(generated_hashes)).encode() + (source_docx or b'')).hexdigest()
    target = cache_dir / ('export-v4-' + identity + path.suffix)
    if not target.is_file():
        _atomic_write(target, clean_export(data, path.suffix, generated_hashes, source_docx))
    return target
