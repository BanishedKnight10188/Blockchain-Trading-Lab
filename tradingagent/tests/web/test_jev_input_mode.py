"""Normal workbench entry and paused input migration; offline and no model fees."""

import json
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.domain.jev_tasks import JevTaskCreate
from agent_platform.web.app import create_app
from tests.integration.test_jev_tasks import action, selection
from tests.web.test_jev_parallel_controls import headers


class InputSelector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.default = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.active = attrs.get("name") == "context_mode"
        if tag == "option" and self.active and "selected" in attrs:
            self.default = attrs["value"]

    def handle_endtag(self, tag):
        if tag == "select":
            self.active = False


def config():
    return RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)


def switch(view, mode, revision=None):
    return action(view) | {
        "context_mode": mode,
        "context_revision": view["task"].get("context_revision", 0)
        if revision is None
        else revision,
    }


def test_normal_wizard_defaults_to_six_layers(tmp_path):
    with TestClient(
        create_app(tmp_path / "root.sqlite3", runtime_config=config()),
        base_url="http://127.0.0.1",
    ) as client:
        parser = InputSelector()
        parser.feed(client.get("/workbench").text)
        assert parser.default == "multiscale"
        assert 'id="save-input-mode"' in client.get("/workbench").text


def test_switch_preserves_wallet_identity_and_is_durable(tmp_path):
    root = tmp_path / "root.sqlite3"
    app = create_app(root, runtime_config=config())
    original = selection("old-create") | {"start": False}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        old = client.post("/api/jev-tasks", headers=auth, json=original).json()
        other = client.post(
            "/api/jev-tasks", headers=auth, json=selection("other") | {"start": False}
        ).json()
        task_id = old["task"]["task_id"]
        path = f"/api/jev-tasks/{task_id}/context"
        changed = client.put(path, headers=auth, json=switch(old, "multiscale"))
        assert changed.status_code == 200, changed.text
        new = changed.json()
        assert new["context_mode"] == "multiscale"
        assert new["task"]["context_revision"] == 1
        assert {k: v for k, v in new["account"].items() if k != "captured_at"} == {
            k: v for k, v in old["account"].items() if k != "captured_at"
        }
        assert new["session"] == old["session"]
        assert not new["paid_models_enabled"]
        assert new["archive_summary"] == old["archive_summary"]
        same = client.put(path, headers=auth, json=switch(new, "multiscale"))
        assert same.status_code == 200
        assert same.json()["task"]["context_revision"] == 1
        row = client.portal.call(app.state.jev_tasks.store.get, task_id)
        assert row.selection == JevTaskCreate.model_validate(original)
        assert client.put(path, headers=auth, json=switch(new, "legacy", 0)).status_code == 409
        assert client.put(path, json=switch(new, "legacy")).status_code == 403
        assert client.put(path, headers=auth, json=switch(new, "unknown")).status_code == 422
        retried = client.post("/api/jev-tasks", headers=auth, json=original).json()
        assert retried["task"]["task_id"] == task_id
        assert retried["context_mode"] == "multiscale"
        assert (
            client.get(f"/api/jev-tasks/{other['task']['task_id']}").json()["context_mode"]
            == "legacy"
        )
    with TestClient(
        create_app(root, runtime_config=config()), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        restored = client.get(f"/api/jev-tasks/{task_id}").json()
        assert restored["context_mode"] == "multiscale"
        assert restored["task"]["context_revision"] == 1
        assert restored["account"]["scope"] == old["account"]["scope"]
        back = client.put(path, headers=auth, json=switch(restored, "legacy"))
        assert back.status_code == 200, back.text
        assert back.json()["context_mode"] == "legacy"
        assert back.json()["multiscale_context"] is None


def test_running_switch_rejected_then_worker_sends_actual_six_layers(tmp_path):
    app = create_app(tmp_path / "root.sqlite3", runtime_config=config())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        old = client.post("/api/jev-tasks", headers=auth, json=selection("active")).json()
        task_id = old["task"]["task_id"]
        path = f"/api/jev-tasks/{task_id}"
        blocked = client.put(path + "/context", headers=auth, json=switch(old, "multiscale"))
        assert blocked.status_code == 409
        assert "暂停" in blocked.json()["detail"]
        old = client.get(path).json()
        paused = client.post(path + "/pause", headers=auth, json=action(old)).json()
        changed = client.put(path + "/context", headers=auth, json=switch(paused, "multiscale"))
        assert changed.status_code == 200, changed.text
        ready = changed.json()
        started = client.post(path + "/start", headers=auth, json=action(ready))
        assert started.status_code == 200, started.text

        async def actual_request():
            import asyncio

            _, services, session = await app.state.jev_tasks.context(task_id)
            svc = services.futures_trading
            async with asyncio.timeout(5):
                while True:
                    for cycle in await svc.store.recent(svc.scope(session).account_ref):
                        if cycle.model_request:
                            state = json.loads(cycle.model_request.state_json)
                            if "market_context" in state:
                                return state
                    await asyncio.sleep(0.1)

        state = client.portal.call(actual_request)
        assert state["market_context"]["version"] == "jev-six-layer-v1"
        assert len(state["market_context"]["short_3m"]["candles"]) == 20
        assert len(state["market_context"]["fast_1s"]["candles"]) == 60
        assert "history" not in state and "recent_quote_ticks" not in state


def test_invalid_background_config_leaves_original_input_and_wallet(tmp_path):
    background = tmp_path / "background.json"
    background.write_text("invalid", encoding="utf-8")
    app = create_app(
        tmp_path / "root.sqlite3",
        runtime_config=config().model_copy(update={"background_model_config": background}),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        old = client.post(
            "/api/jev-tasks", headers=auth, json=selection("bad-bg") | {"start": False}
        ).json()
        path = f"/api/jev-tasks/{old['task']['task_id']}"
        response = client.put(path + "/context", headers=auth, json=switch(old, "multiscale"))
        assert response.status_code == 409
        current = client.get(path).json()
        assert current["context_mode"] == "legacy"
        assert current["task"]["context_revision"] == 0
        assert current["account"]["scope"] == old["account"]["scope"]
        assert current["account"]["free_usdt"] == old["account"]["free_usdt"]
