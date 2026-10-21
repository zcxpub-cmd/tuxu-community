"""Community-specific public-template and standalone installation checks."""
from pathlib import Path

from backend.app import community_prompts
from backend.app.director import STYLES, BRIGHTNESS, default_director
from backend.app.main import default_data_directory
from backend.app.models import ModelError, OllamaAdapter
import pytest


def test_community_templates_are_public_short_protocols():
    for name in ('ANALYSIS', 'SYNTHESIS', 'SCENE', 'OBJECT', 'BACKGROUND', 'REPAIR'):
        text = getattr(community_prompts, name)
        assert isinstance(text, str) and 0 < len(text) < 500
    assert len(STYLES) == 9 and len(BRIGHTNESS) == 3
    prompt = community_prompts.basic_cloud_prompts([{'id': 's', 'description': '一棵树。',
                                                    'director': default_director()}], '简洁插画')['s']
    assert 'tuxu-community-v1' in prompt['text'] and '一棵树。' in prompt['text']


def test_community_default_directory_is_independent_and_honors_override(monkeypatch, tmp_path):
    monkeypatch.delenv('TUXU_DATA_DIR', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr('backend.app.main.sys.platform', 'darwin')
    assert default_data_directory() == tmp_path / 'Library/Application Support/TuxuCommunity/data'
    monkeypatch.setattr('backend.app.main.sys.platform', 'linux')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
    assert default_data_directory() == tmp_path / 'xdg/TuxuCommunity/data'
    monkeypatch.setenv('TUXU_DATA_DIR', str(tmp_path / 'explicit'))
    assert default_data_directory() == tmp_path / 'explicit'


def test_community_bootstrap_identity_and_default_are_standalone(harness):
    client, service, _text, _image = harness
    response = client.get('/api/bootstrap').json()
    assert response['application'] == {'id': 'tuxu-community', 'version': '0.1.0'}
    assert client.app.version == '0.1.0'
    assert response['defaults']['image_provider'] == 'local'
    assert not service.store.list()


@pytest.mark.parametrize('coverage', [['heading', 'body'], ['body'], ['heading', 'heading'],
                                     [{'id': 'heading'}, {'id': 'body'}]])
def test_analysis_uses_explicit_schema_but_keeps_semantic_coverage_check(coverage):
    paragraphs = [{'id': 'heading', 'kind': 'heading', 'text': '社区样稿'},
                  {'id': 'body', 'kind': 'paragraph', 'text': '雨后的一张书桌。'}]
    class Adapter(OllamaAdapter):
        def _chat(self, model, system, payload, output_format='json'):
            self.schema = output_format
            return {'summary': '社区样稿', 'coverage': coverage, 'slots': []}
    adapter = Adapter()
    if coverage == ['heading', 'body']:
        result = adapter.analyze({'paragraphs': paragraphs}, 'report', '基础插画', 'mock')
        assert result['coverage'] == coverage
    else:
        with pytest.raises(ModelError):
            adapter.analyze({'paragraphs': paragraphs}, 'report', '基础插画', 'mock')
    schema = adapter.schema
    assert schema['required'] == ['summary', 'coverage', 'slots']
    assert schema['properties']['coverage'] == {'type': 'array', 'minItems': 2, 'maxItems': 2,
        'uniqueItems': True, 'items': {'type': 'string', 'enum': ['heading', 'body']}}
    slots = schema['properties']['slots']
    assert slots['items']['properties']['paragraph_id']['enum'] == ['body']
    assert set(slots['items']['required']) == {'paragraph_id', 'reason', 'description', 'caption', 'risk', 'aspect_ratio'}


def test_analysis_actual_http_format_is_schema_and_uses_deterministic_mode(monkeypatch):
    import json
    import httpx
    original = httpx.Client
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop',
            'message': {'content': json.dumps({'summary': '无插图', 'coverage': ['p'], 'slots': []})}})
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=transport, **kwargs))
    paragraph = {'id': 'p', 'kind': 'paragraph', 'text': '社区短稿。'}
    result = OllamaAdapter().analyze({'paragraphs': [paragraph]}, 'report', '插画', 'mock')
    assert result['coverage'] == ['p']
    assert requests[0]['format'] == OllamaAdapter.analysis_schema(OllamaAdapter.chunks([paragraph])[0])
    assert requests[0]['options']['temperature'] == 0 and requests[0]['stream'] is False
    assert requests[0]['keep_alive'] == 0


# Reuse only the isolated mock-service fixture, never the actual user's Store.
from backend.tests.test_service import docs_double, harness
