"""A confirmed operation stays bound to the account and style shown to the user."""

from fastapi.testclient import TestClient

from tests.web.test_paper_routes import configuration, configured_app, operation, setup


def test_legacy_unbound_action_cannot_start_another_wallet(tmp_path):
    app = configured_app(tmp_path / "scope.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = setup(client)
        old = client.post("/api/paper/configure", headers=auth, json=configuration(client)).json()[
            "account"
        ]
        current = client.get("/api/session").json()["session"]
        assert (
            client.put(
                f"/api/sessions/{current['session_id']}/state",
                headers=auth,
                json={"status": "closed", "expected_revision": current["revision"]},
            ).status_code
            == 200
        )
        client.post(
            "/api/sessions", headers=auth, json={"style_strength": 50, "style_confirmed": True}
        )
        new = client.post("/api/paper/configure", headers=auth, json=configuration(client)).json()[
            "account"
        ]
        assert old["revision"] == new["revision"]
        assert (
            client.post(
                "/api/paper/start",
                headers=auth,
                json={"expected_revision": old["revision"], "confirmed": True},
            ).status_code
            >= 400
        )
        assert (
            client.post(
                "/api/paper/start",
                headers=auth,
                json={
                    "expected_revision": old["revision"],
                    "account_ref": old["account_ref"],
                    "activation_revision": old["activation_revision"],
                    "style_revision": 1,
                    "confirmed": True,
                },
            ).status_code
            == 409
        )
        assert client.get("/api/paper").json()["account"]["status"] == "paused"


def test_pause_keeps_activation_identity_while_balances_advance(tmp_path):
    app = configured_app(tmp_path / "pause.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = setup(client)
        account = client.post(
            "/api/paper/configure", headers=auth, json=configuration(client)
        ).json()["account"]
        started = client.post(
            "/api/paper/start", headers=auth, json=operation(client, account)
        ).json()["account"]
        old_pause = operation(client, started)
        client.portal.call(app.state.services.paper.step)
        paused = client.post("/api/paper/pause", headers=auth, json=old_pause)
        assert paused.status_code == 200
        restarted = client.post(
            "/api/paper/start", headers=auth, json=operation(client, paused.json()["account"])
        )
        assert restarted.status_code == 200
        assert client.post("/api/paper/pause", headers=auth, json=old_pause).status_code == 409
        assert client.get("/api/paper").json()["account"]["status"] == "running"
