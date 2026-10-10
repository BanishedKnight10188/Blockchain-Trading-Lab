from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app
from tests.fixtures.event_agent_cases import lane
from tests.web.test_jev_parallel_controls import headers


def test_csrf_revision_and_status(tmp_path):
    app = create_app(tmp_path / "web.sqlite", runtime_config=RuntimeConfig(event_agent=True))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        status = client.get("/api/event-agent/status")
        assert status.status_code == 200 and status.json()["execution_environment"] == "paper"
        value = status.json()["lane"]
        config = lane().model_dump(mode="json")
        selected = {
            k: config[k]
            for k in ("policy", "limits", "qty_step", "min_qty", "max_qty", "min_notional")
        }
        body = {"expected_revision": value["revision"], "confirmed": True, **selected}
        assert client.post("/api/event-agent/configure", json=body).status_code == 403
        configured = client.post("/api/event-agent/configure", headers=auth, json=body)
        assert configured.status_code == 200, configured.text
        revision = configured.json()["revision"]
        assert (
            client.post(
                "/api/event-agent/start",
                headers=auth,
                json={"expected_revision": revision - 1, "confirmed": True},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/event-agent/start",
                headers=auth,
                json={"expected_revision": revision, "confirmed": True},
            ).status_code
            == 200
        )
        state = client.get("/api/event-agent/status").json()
        assert state["paid_calls_enabled"] is False
        research = client.post(
            "/api/event-agent/analyze",
            headers=auth,
            json={
                "expected_revision": state["lane"]["revision"],
                "request_id": "research-1",
                "confirmed": True,
            },
        )
        assert research.status_code == 200, research.text
        assert research.json()["status"] == "WAIT"
        assert client.get("/event-agent").status_code == 200
    with TestClient(
        create_app(tmp_path / "web.sqlite", runtime_config=RuntimeConfig(event_agent=True)),
        base_url="http://127.0.0.1",
    ) as client:
        headers(client)
        state = client.get("/api/event-agent/status").json()
        assert state["lane"]["enabled"] is False
        assert len(state["runs"]) == 1


def test_disabled_by_default(tmp_path):
    with TestClient(create_app(tmp_path / "default.sqlite"), base_url="http://127.0.0.1") as client:
        headers(client)
        assert client.get("/api/event-agent/status").json()["available"] is False
