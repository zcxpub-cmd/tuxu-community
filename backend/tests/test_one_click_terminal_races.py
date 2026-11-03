"""Cross-module cancellation/restart windows, using only isolated synthetic data."""
import hashlib
import threading
import time

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.tests.test_one_click import harness, imported, start, terminal
from backend.tests.test_service import FakeComfy, FakeText


def await_condition(condition, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.01)
    raise AssertionError('Synthetic race did not settle')


def test_cancel_during_prompt_return_cannot_create_late_approval(harness, monkeypatch):
    client, service, text, comfy = harness
    entered, release = threading.Event(), threading.Event()
    original = text.prompts

    def blocked(*args):
        result = original(*args)
        entered.set()
        assert release.wait(4)
        return result

    monkeypatch.setattr(text, 'prompts', blocked)
    task = start(client, imported(client))
    try:
        assert entered.wait(4)
        assert client.post(f'/api/tasks/{task["id"]}/cancel', json={}).status_code == 200
    finally:
        release.set()
    await_condition(lambda: not service.require(task['id']).get('_prompting'))
    service.one_click.pump()
    final = service.require(task['id'])
    assert final['automation']['status'] == 'cancelled'
    assert final['approval'] is None and final['selections'] == {} and final['exports'] == []
    assert text.calls == text.prompt_calls == 1 and comfy.calls == 0


def test_cancel_before_queued_generation_runs_prevents_first_image(harness, monkeypatch):
    client, service, text, comfy = harness
    queued = []
    original = service.schedule

    def defer_image(task_id, job_id):
        job = next(job for job in service.require(task_id)['jobs'] if job['id'] == job_id)
        if job['kind'] == 'generation':
            queued.append(job_id)
        else:
            original(task_id, job_id)

    monkeypatch.setattr(service, 'schedule', defer_image)
    task = start(client, imported(client))
    await_condition(lambda: bool(queued))
    assert client.post(f'/api/tasks/{task["id"]}/cancel', json={}).status_code == 200
    service.run_job(task['id'], queued[0])
    service.one_click.pump()
    final = service.require(task['id'])
    assert final['automation']['status'] == 'cancelled'
    assert final['jobs'][-1]['status'] == 'cancelled'
    assert final['assets'] == final['exports'] == [] and final['selections'] == {}
    assert text.calls == text.prompt_calls == 1 and comfy.calls == 0


def test_cancel_after_selection_snapshot_prevents_selected_assets_commit(harness):
    client, service, _, comfy = harness
    original = service.one_click.actions['selection']
    reached = []

    def cancel_then_select(task_id, body):
        reached.append(True)
        assert body['selections']
        assert client.post(f'/api/tasks/{task_id}/cancel', json={}).status_code == 200
        return original(task_id, body)

    service.one_click.actions['selection'] = cancel_then_select
    task = terminal(client, start(client, imported(client))['id'])
    assert reached == [True] and task['automation']['status'] == 'cancelled'
    assert len(task['assets']) == comfy.calls == 1
    assert task['selections'] == {} and task['exports'] == []


def test_cancel_while_export_holds_store_lock_prevents_late_publication(harness, monkeypatch):
    from backend.app import documents

    client, service, _, comfy = harness
    original = documents.export_document
    cancel_threads, responses, rendered = [], [], []

    def render_then_cancel(*args, **kwargs):
        output = original(*args, **kwargs)
        rendered.append(output)
        task = service.store.list()[0]
        event = service.one_click._event(task['id'], task['automation']['id'])
        thread = threading.Thread(target=lambda: responses.append(
            client.post(f'/api/tasks/{task["id"]}/cancel', json={})), daemon=True)
        cancel_threads.append(thread)
        thread.start()
        # Cancel cannot acquire the export's Store lock yet; its pre-lock signal
        # must nevertheless prevent the final published export transaction.
        assert event.wait(4)
        return output

    monkeypatch.setattr(documents, 'export_document', render_then_cancel)
    task = start(client, imported(client))
    final = terminal(client, task['id'])
    for thread in cancel_threads:
        thread.join(4)
        assert not thread.is_alive()
    final = client.get('/api/tasks/' + task['id']).json()
    assert rendered and responses[0].status_code == 200
    assert final['automation']['status'] == 'cancelled'
    assert final['exports'] == [] and 'export_url' not in final['automation']
    assert len(final['assets']) == comfy.calls == 1


def test_restart_after_recorded_export_finishes_without_second_export_or_model(harness):
    client, service, text, comfy = harness
    service.one_click.stop()
    service.one_click.stop_event.clear()
    task = start(client, imported(client))

    def export_recorded():
        service.one_click.pump()
        current = service.require(task['id'])
        action = current['automation']['_actions'].get('export', {})
        return action.get('state') == 'recorded'

    await_condition(export_recorded)
    before = service.require(task['id'])
    assert before['automation']['stage'] == 'export' and before['automation']['status'] == 'running'
    assert before['status'] == 'exported' and len(before['exports']) == 1
    filename = before['exports'][0]['filename']
    path = service.directory(task['id']) / 'downloads' / filename
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    replacement_text, replacement_comfy = FakeText(), FakeComfy()
    replacement = create_app(service.store.directory, replacement_text, replacement_comfy)
    with TestClient(replacement, base_url='http://127.0.0.1:8765') as reloaded:
        reloaded.headers['X-Local-Token'] = reloaded.get('/api/bootstrap').json()['token']
        final = terminal(reloaded, task['id'])
        assert final['automation']['status'] == 'completed' and final['status'] == 'exported'
        assert final['exports'] == before['exports'] and len(final['jobs']) == len(before['jobs'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
        assert replacement_text.calls == replacement_text.prompt_calls == replacement_comfy.calls == 0
        assert text.calls == text.prompt_calls == comfy.calls == 1
