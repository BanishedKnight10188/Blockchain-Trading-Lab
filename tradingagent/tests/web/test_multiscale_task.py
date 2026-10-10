from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.domain.jev_tasks import JevTaskCreate
from agent_platform.web.app import create_app
from tests.integration.test_jev_tasks import selection
from tests.web.test_jev_parallel_controls import headers


def test_background_json_config_accepts_verified_decimal_prices_and_timestamp(tmp_path):
    config_path = tmp_path / "background.json"
    config_path.write_text(
        '{"enabled":true,"providers":["wafer"],"price":{"version":"verified",'
        '"input_usd_per_million":"0.03","output_usd_per_million":"0.40",'
        '"verified_at":"2026-10-09T03:34:00Z","valid_until":null}}',
        encoding="utf-8",
    )
    app = create_app(
        tmp_path / "root.sqlite3",
        runtime_config=RuntimeConfig(
            paper=True, paper_mock=True, jev_workbench=True, background_model_config=config_path
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        response = client.post(
            "/api/jev-tasks",
            headers=auth,
            json=selection("configured") | {"context_mode": "multiscale", "start": False},
        )
        assert response.status_code == 201, response.text
        assert response.json()["multiscale_context"]["background_refresh_seconds"] == 900


def test_new_mode_is_task_scoped_and_legacy_identity_preserved(tmp_path):
    legacy = JevTaskCreate.model_validate(selection("legacy"))
    assert "context_mode" not in legacy.model_dump()
    config = RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)
    app = create_app(tmp_path / "root.sqlite3", runtime_config=config)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        raw = selection("multi") | {"context_mode": "multiscale", "start": False}
        response = client.post("/api/jev-tasks", headers=auth, json=raw)
        assert response.status_code == 201, response.text
        view = response.json()
        assert view["context_mode"] == "multiscale"
        assert JevTaskCreate.model_validate(raw).context_mode == "multiscale"
        assert view["multiscale_context"]["background_refresh_seconds"] == 900
        old = client.post("/api/jev-tasks", headers=auth, json=selection("old") | {"start": False})
        assert old.status_code == 201
        assert old.json()["context_mode"] == "legacy"
        assert old.json()["multiscale_context"] is None
        same = client.post("/api/jev-tasks", headers=auth, json=raw)
        assert same.json()["task"]["task_id"] == view["task"]["task_id"]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers(client)
        restored = client.get("/api/jev-tasks/" + view["task"]["task_id"])
        assert restored.status_code == 200
        assert restored.json()["context_mode"] == "multiscale"
