"""Every image route must compose text locally before any image submission."""
import pytest

from backend.tests.test_cloud_service import harness
from backend.tests.test_service import docs_double, ready


@pytest.mark.parametrize('provider', ['local', 'ark', 'openai', 'codex'])
@pytest.mark.parametrize('model_fails', [False, True])
def test_all_image_routes_use_local_model_and_never_fallback(harness, monkeypatch, provider, model_fails):
    client, service, text, comfy = harness
    task = ready(harness)
    if provider == 'openai':
        service.cloud_config.update('openai', {'enabled': True, 'api_key': 'FAKE_LOCAL_ONLY_KEY'})
    seen = []
    original = text.prompts
    def local(slots, style, model):
        seen.append((len(slots), model))
        if model_fails:
            raise RuntimeError('PRIVATE_MODEL_FAILURE')
        return original(slots, style, model)
    monkeypatch.setattr(text, 'prompts', local)
    response = client.post('/api/tasks/' + task['id'] + '/approve', json={
        'version': task['plan']['version'], 'checkpoint': 'fake-checkpoint',
        'image_provider': provider, 'max_attempts': 3, 'confirm': True})
    assert seen == [(len(task['plan']['slots']), 'fake-text')]
    assert service.cloud.calls == 0 and comfy.calls == 0
    saved = service.require(task['id'])
    if model_fails:
        assert response.status_code == 422
        assert saved['approval'] is None
        assert not saved.get('_prompting')
        assert 'PRIVATE_MODEL_FAILURE' not in response.text
    else:
        assert response.status_code == 200
        assert saved['approval']['prompt_source'] == 'ollama_local_v2'
        assert saved['prompts']
        assert 'prompts' not in response.json()
