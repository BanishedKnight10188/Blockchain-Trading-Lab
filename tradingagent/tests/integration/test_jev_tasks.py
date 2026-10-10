"""Independent task identities; offline ASGI/SQLite only, no paid inference."""

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app
from tests.domain.test_futures_paper import settings_data
from tests.integration.test_futures_trading_core import configured
from tests.web.test_jev_parallel_controls import headers

pytest_plugins = ["tests.integration.test_futures_trading_core"]


def selection(key, symbol="BTCUSDT", kind="jev_trader"):
    return {
        "request_id": key,
        "kind": kind,
        "name": symbol + (" 操盘" if kind == "jev_trader" else " 建议"),
        "analysis_target": {"market": "usdt_perpetual", "symbol": symbol, "history_days": 1},
        "style_strength": 85,
        "style_confirmed": True,
        "limits": settings_data(),
        "policy": {
            "order_notional_usdt": "2000",
            "max_price_drift_bps": "20",
            "min_confidence": "0.8",
            "strategy_instructions": "Observe market and manage exits.",
        },
        "start": True,
        "confirmed": True,
    }


def action(view):
    return {
        "confirmed": True,
        "expected_revision": view["account"]["revision"],
        "session_revision": view["session"]["revision"],
    }


@pytest.mark.asyncio
async def test_advice_records_non_wait_without_submitting_any_trade(assembled):
    svc, clock, _, _ = assembled
    svc.advisory_only = True
    run = await configured(svc)
    svc.model.choices = ("OPEN_LONG",)
    cycle = await svc.step()
    assert cycle.status == "advised" and cycle.decision == "OPEN_LONG"
    assert cycle.command_id is None and cycle.model_request and cycle.model_response
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.quantity == 0 and account.free_usdt == 1000
    assert (await svc.public_view())["operation_mode"] == "advice"


def test_two_background_tasks_idempotence_pause_isolation_and_recovery(tmp_path):
    config = RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)
    app = create_app(tmp_path / "root.sqlite3", runtime_config=config)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = headers(client)
        assert client.post("/api/jev-tasks", json=selection("no-csrf")).status_code == 403
        first = client.post("/api/jev-tasks", headers=auth, json=selection("task-a"))
        assert first.status_code == 201, first.text
        a = first.json()
        second = client.post(
            "/api/jev-tasks", headers=auth, json=selection("task-b", "ETHUSDT", "jev_advice")
        )
        assert second.status_code == 201, second.text
        b = second.json()
        assert all(
            service.sessions.clock is app.state.services.sessions.clock
            for service in app.state.jev_tasks.contexts.values()
        )
        assert a["session"]["session_id"] != b["session"]["session_id"]
        assert a["account"]["status"] == b["account"]["status"] == "running"
        duplicate = client.post("/api/jev-tasks", headers=auth, json=selection("task-a")).json()
        assert duplicate["task"]["task_id"] == a["task"]["task_id"]
        assert len(client.get("/api/jev-tasks").json()["tasks"]) == 2
        assert (
            client.post(
                "/api/jev-tasks", headers=auth, json=selection("task-a", "ETHUSDT")
            ).status_code
            == 409
        )
        client.portal.call(asyncio.sleep, 1.3)
        a = client.get("/api/jev-tasks/" + a["task"]["task_id"]).json()
        b = client.get("/api/jev-tasks/" + b["task"]["task_id"]).json()
        assert a["cycles"] and b["cycles"]
        paused = client.post(
            "/api/jev-tasks/" + a["task"]["task_id"] + "/pause", headers=auth, json=action(a)
        )
        assert paused.status_code == 200, paused.text
        retried = client.post("/api/jev-tasks", headers=auth, json=selection("task-a")).json()
        assert retried["account"]["status"] == "paused"
        assert retried["task"]["task_id"] == a["task"]["task_id"]
        assert (
            client.get("/api/jev-tasks/" + b["task"]["task_id"]).json()["account"]["status"]
            == "running"
        )
        assert client.get("/api/jev-tasks/not-a-file").status_code == 404
    with TestClient(
        create_app(tmp_path / "root.sqlite3", runtime_config=config), base_url="http://127.0.0.1"
    ) as client:
        headers(client)
        tasks = client.get("/api/jev-tasks").json()["tasks"]
        assert len(tasks) == 2
        for row in tasks:
            view = client.get("/api/jev-tasks/" + row["task_id"]).json()
            assert view["account"]["status"] == "paused"
            assert Decimal(view["account"]["free_usdt"]) == 1000
            assert not view["paid_models_enabled"]


def test_real_task_config_cannot_reset_original_model_budget(tmp_path):
    from agent_platform.application.jev_tasks import JevTaskWorkbench

    root = tmp_path / "wallet.sqlite3"
    config = RuntimeConfig(
        paper=True,
        live_public=True,
        paper_model_config=tmp_path / "policy.json",
        jev_workbench=True,
    )
    manager = JevTaskWorkbench(root, config, None)
    assert manager.task_config().model_budget_database == root.resolve()
    shared = tmp_path / "original-fees.sqlite3"
    config = config.model_copy(update={"model_budget_database": shared})
    assert JevTaskWorkbench(root, config, None).task_config().model_budget_database == shared


def test_task_cadence_create_update_isolation_and_restart(tmp_path):
    config = RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)
    path = tmp_path / "cadence.sqlite3"
    with TestClient(create_app(path, runtime_config=config), base_url="http://127.0.0.1") as client:
        auth = headers(client)
        a = client.post(
            "/api/jev-tasks",
            headers=auth,
            json=selection("cadence-a") | {"start": False, "decision_interval_seconds": 3},
        )
        assert a.status_code == 201, a.text
        a = a.json()
        b = client.post(
            "/api/jev-tasks",
            headers=auth,
            json=selection("cadence-b", "ETHUSDT") | {"start": False},
        ).json()
        url = "/api/jev-tasks/" + a["task"]["task_id"] + "/cadence"
        assert a["cadence"]["decision_seconds"] == 3
        body = action(a) | {
            "cadence_revision": a["task"]["cadence_revision"],
            "decision_interval_seconds": 10,
        }
        assert client.put(url, json=body).status_code == 403
        changed = client.put(url, headers=auth, json=body)
        assert changed.status_code == 200, changed.text
        changed = changed.json()
        assert changed["cadence"]["decision_seconds"] == 10
        assert changed["cadence"]["maintenance_seconds"] == 1
        assert changed["cadence"]["prediction_ttl_seconds"] == 3
        assert changed["task"]["cadence_revision"] == body["cadence_revision"] + 1
        service = client.app.state.jev_tasks.contexts[a["task"]["task_id"]].futures_trading
        assert service.cadence_runtime.decision_seconds == 10
        assert client.put(url, headers=auth, json=body).status_code == 409
        assert (
            client.get("/api/jev-tasks/" + b["task"]["task_id"]).json()["cadence"][
                "decision_seconds"
            ]
            == 1
        )
        for bad in (0, 11, True, 1.5):
            invalid = selection("invalid-" + str(bad)) | {"decision_interval_seconds": bad}
            assert client.post("/api/jev-tasks", headers=auth, json=invalid).status_code == 422
        a_id = a["task"]["task_id"]
    with TestClient(create_app(path, runtime_config=config), base_url="http://127.0.0.1") as client:
        headers(client)
        restored = client.get("/api/jev-tasks/" + a_id).json()
        assert restored["cadence"]["decision_seconds"] == 10
        assert restored["task"]["cadence_revision"] == changed["task"]["cadence_revision"]
        assert not restored["paid_models_enabled"]


def test_running_task_cadence_cannot_change_or_rewrite_creation_intent(tmp_path):
    config = RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)
    with TestClient(
        create_app(tmp_path / "running.sqlite3", runtime_config=config), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        original = selection("original-intent") | {"decision_interval_seconds": 3}
        a = client.post("/api/jev-tasks", headers=auth, json=original).json()
        url = "/api/jev-tasks/" + a["task"]["task_id"]
        response = client.put(
            url + "/cadence",
            headers=auth,
            json=action(a)
            | {"cadence_revision": a["task"]["cadence_revision"], "decision_interval_seconds": 5},
        )
        assert response.status_code == 409
        assert client.get(url).json()["cadence"]["decision_seconds"] == 3
        duplicate = client.post("/api/jev-tasks", headers=auth, json=original)
        assert (
            duplicate.status_code == 201
            and duplicate.json()["task"]["task_id"] == a["task"]["task_id"]
        )


def test_import_closed_legacy_and_task_versions_do_not_change_other_sessions(tmp_path):
    config = RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)
    with TestClient(
        create_app(tmp_path / "root.sqlite3", runtime_config=config), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        original = client.post(
            "/api/sessions",
            headers=auth,
            json={
                "style_strength": 85,
                "style_confirmed": True,
                "analysis_target": {"market": "usdt_perpetual", "symbol": "BTCUSDT"},
            },
        ).json()["session"]
        client.put(
            "/api/sessions/" + original["session_id"] + "/state",
            headers=auth,
            json={"status": "closed", "expected_revision": original["revision"]},
        )
        rows = client.get("/api/jev-tasks").json()["tasks"]
        assert rows[0]["legacy"] and rows[0]["status"] == "closed"
        legacy = client.get("/api/jev-tasks/" + rows[0]["task_id"]).json()
        assert legacy["readonly"] and legacy["style_strength"] == 85
        assert client.get("/api/jev-tasks/" + rows[0]["task_id"] + "/archive").status_code == 409
        task = client.post(
            "/api/jev-tasks", headers=auth, json=selection("one-new", "ETHUSDT")
        ).json()
        wrong = action(task) | {"session_revision": task["session"]["revision"] + 1}
        assert (
            client.post(
                "/api/jev-tasks/" + task["task"]["task_id"] + "/pause", headers=auth, json=wrong
            ).status_code
            == 409
        )
        assert (
            client.get("/api/jev-tasks/" + task["task"]["task_id"]).json()["account"]["status"]
            == "running"
        )
        assert client.get("/api/jev-tasks/" + rows[0]["task_id"]).json()["readonly"]
        assert client.get("/workbench").status_code == 200
