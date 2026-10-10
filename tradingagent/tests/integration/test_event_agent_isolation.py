from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app
from tests.web.test_paper_routes import setup as setup_jev


def test_controls_are_lane_scoped(tmp_path):
    app = create_app(
        tmp_path / "parallel.sqlite",
        runtime_config=RuntimeConfig(paper=True, paper_mock=True, event_agent=True),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = setup_jev(client)
        before = client.get("/api/controls").json()
        state = client.get("/api/event-agent/status").json()
        revision = state["lane"]["revision"]
        assert (
            client.post(
                "/api/event-agent/start",
                headers=auth,
                json={"expected_revision": revision, "confirmed": True},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/event-agent/pause",
                headers=auth,
                json={"expected_revision": revision + 1, "confirmed": True},
            ).status_code
            == 200
        )
        assert client.get("/api/controls").json() == before
        assert state["lane"]["scope"]["session_id"].startswith("event-agent")
