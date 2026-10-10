"""The slider contract survives validation, concurrent updates and server restart."""

import importlib
import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_factory():
    return importlib.import_module("agent_platform.web.app").create_app


def open_page(client):
    response = client.get("/")
    assert response.status_code == 200
    token = re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)
    return {"Origin": "http://127.0.0.1", "X-CSRF-Token": token}


@pytest.fixture
def context(app_factory, tmp_path):
    with TestClient(app_factory(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        yield client, open_page(client)


def test_page_exposes_a_labelled_integer_slider(context):
    client, _ = context
    page = client.get("/")
    assert 'type="range"' in page.text
    assert 'min="0" max="100" step="1" value="50"' in page.text
    assert 'for="style-strength"' in page.text
    assert "最保守" in page.text and "最激进" in page.text
    assert "行情与账户尚未接入" in page.text
    assert "HttpOnly" in page.headers.get("set-cookie", "") or client.cookies


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"style_strength": 50},
        {"style_strength": 50, "style_confirmed": False},
        {"style_strength": -1, "style_confirmed": True},
        {"style_strength": 101, "style_confirmed": True},
        {"style_strength": 50.0, "style_confirmed": True},
        {"style_strength": True, "style_confirmed": True},
        {"style_strength": "50", "style_confirmed": True},
        {"style_strength": 50, "style_confirmed": 1},
    ],
)
def test_creation_requires_explicit_confirmed_integer_style(context, payload):
    client, headers = context
    assert client.post("/api/sessions", json=payload, headers=headers).status_code == 422
    assert client.get("/api/session").json()["session"] is None


def test_style_save_and_revision_conflict(context):
    client, headers = context
    created = client.post(
        "/api/sessions",
        headers=headers,
        json={
            "style_strength": 0,
            "style_confirmed": True,
        },
    )
    assert created.status_code == 201
    session_id = created.json()["session"]["session_id"]
    payload = {"style_strength": 100, "style_confirmed": True, "expected_revision": 1}
    changed = client.put(f"/api/sessions/{session_id}/style", headers=headers, json=payload)
    assert changed.status_code == 200
    assert changed.json()["session"]["style"]["strength"] == 100
    assert changed.json()["session"]["style_revision"] == 2
    assert [item["strength"] for item in changed.json()["style_history"]] == [0, 100]
    assert (
        client.put(
            f"/api/sessions/{session_id}/style",
            headers=headers,
            json=payload,
        ).status_code
        == 409
    )


def test_saved_style_is_recovered_in_a_new_app(app_factory, tmp_path):
    path = tmp_path / "recover.sqlite3"
    with TestClient(app_factory(path), base_url="http://127.0.0.1") as first:
        headers = open_page(first)
        first.post(
            "/api/sessions",
            headers=headers,
            json={
                "style_strength": 73,
                "style_confirmed": True,
            },
        )
    with TestClient(app_factory(path), base_url="http://127.0.0.1") as second:
        open_page(second)
        recovered = second.get("/api/session").json()
        assert recovered["session"]["style"]["strength"] == 73
        assert len(recovered["style_history"]) == 1


def test_foreign_origin_and_missing_csrf_are_rejected(context):
    client, headers = context
    payload = {"style_strength": 50, "style_confirmed": True}
    assert client.post("/api/sessions", json=payload).status_code == 403
    foreign = {**headers, "Origin": "https://foreign.example"}
    assert client.post("/api/sessions", json=payload, headers=foreign).status_code == 403
    assert client.get("/", headers={"Host": "foreign.example"}).status_code == 400


def test_unknown_sensitive_input_is_not_reflected(context):
    client, headers = context
    secret = "never-reflect-this-input"
    response = client.post(
        "/api/sessions",
        headers=headers,
        json={
            "style_strength": 50,
            "style_confirmed": True,
            "api_secret": secret,
        },
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_only_one_open_session_can_be_created(context):
    client, headers = context
    payload = {"style_strength": 50, "style_confirmed": True}
    assert client.post("/api/sessions", headers=headers, json=payload).status_code == 201
    assert client.post("/api/sessions", headers=headers, json=payload).status_code == 409
