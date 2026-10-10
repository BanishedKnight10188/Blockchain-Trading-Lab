"""Protected selection uses server catalog and preserves saved market identity."""

import re

import pytest
from fastapi.testclient import TestClient

from agent_platform.adapters.fake.session_analysis import FakeHistoricalMarket
from agent_platform.web.app import create_app


@pytest.fixture
def web(tmp_path):
    with TestClient(create_app(tmp_path / "multi.sqlite3"), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        headers = {
            "Origin": "http://127.0.0.1",
            "X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1),
        }
        yield client, headers


def payload(symbol="ETHUSDT"):
    return {
        "style_strength": 80,
        "style_confirmed": True,
        "analysis_target": {
            "market": "usdt_perpetual",
            "symbol": symbol,
            "history_days": 7,
            "interval": "1h",
        },
    }


def connect(client):
    svc = client.app.state.services.initial_analysis
    svc.history = FakeHistoricalMarket(svc.clock)
    return svc


def test_page_has_market_symbol_and_history_selection(web):
    client, _ = web
    page = client.get("/").text
    assert 'id="analysis-market"' in page and 'id="analysis-symbol"' in page
    assert 'id="history-days"' in page and "USDT 永续" in page
    assert 'id="initial-analysis-panel"' in client.get("/overview").text


def test_disabled_public_catalog_never_fakes_symbol_list(web):
    client, headers = web
    assert client.get("/api/analysis/contracts").status_code == 503
    assert client.post("/api/sessions", json=payload(), headers=headers).status_code == 503
    assert client.get("/api/session").json()["session"] is None


def test_catalog_error_logs_only_bounded_public_reason(web, caplog):
    from agent_platform.adapters.binance_direct.futures_public import FuturesPublicError

    client, _ = web
    svc = connect(client)

    async def bad():
        raise FuturesPublicError("invalid_contract_catalog")

    svc.history.catalog = bad
    assert client.get("/api/analysis/contracts").status_code == 503
    assert "invalid_contract_catalog" in caplog.text
    caplog.clear()

    async def unknown():
        raise FuturesPublicError("private_payload_must_not_escape")

    svc.history.catalog = unknown
    assert client.get("/api/analysis/contracts").status_code == 503
    assert "private_payload_must_not_escape" not in caplog.text


def test_symbol_validated_and_identity_saved_without_csrf_bypass(web):
    client, headers = web
    svc = connect(client)
    catalog = client.get("/api/analysis/contracts").json()
    assert {item["symbol"] for item in catalog["contracts"]} == {
        "BTCUSDT",
        "ETHUSDT",
        "1000SHIBUSDT",
    }
    assert client.post("/api/sessions", json=payload()).status_code == 403
    assert (
        client.post("/api/sessions", json=payload("NOTLISTEDUSDT"), headers=headers).status_code
        == 422
    )
    response = client.post("/api/sessions", json=payload(), headers=headers)
    assert response.status_code == 201
    session = response.json()["session"]
    assert session["analysis_target"]["symbol"] == "ETHUSDT" and session["style"]["strength"] == 80
    client.portal.call(svc.step)
    current = client.get("/api/analysis/current").json()
    assert current["record"]["status"] == "model_unconfigured"
    assert current["record"]["history"]["source"] == "fake"
    assert len(current["record"]["history"]["candles"]) == 168
    bad = {
        "style_strength": 20,
        "style_confirmed": True,
        "expected_revision": 1,
        "analysis_target": payload("BTCUSDT")["analysis_target"],
    }
    assert (
        client.put(
            f"/api/sessions/{session['session_id']}/style", json=bad, headers=headers
        ).status_code
        == 422
    )
    assert client.get("/api/session").json()["session"]["analysis_target"]["symbol"] == "ETHUSDT"


def test_legacy_create_stays_spot_and_no_initial_history_or_fee(web):
    client, headers = web
    result = client.post(
        "/api/sessions", json={"style_strength": 80, "style_confirmed": True}, headers=headers
    )
    assert (
        result.status_code == 201
        and result.json()["session"]["analysis_target"]["market"] == "spot"
    )
    assert client.get("/api/analysis/current").json()["reason"] == "legacy_spot"


def test_futures_overview_never_projects_spot_data_source(web):
    from agent_platform.domain.overview import OverviewFrame

    client, headers = web
    svc = connect(client)
    frame = OverviewFrame(
        mode="fake", market_source="fake", captured_at=svc.clock.utcnow(), market_error="transport"
    )

    class SpotSource:
        async def latest(self):
            return frame

    client.app.state.services.queries.source = SpotSource()
    assert client.post("/api/sessions", json=payload(), headers=headers).status_code == 201
    overview = client.get("/api/overview").json()
    assert overview["market"]["source"] == "none" and overview["market"]["price"] is None
    assert overview["account"]["source"] == "none" and not overview["account"]["balances"]
    assert overview["advice"] is None


def test_disabled_paper_still_identifies_futures_session_and_spot_wallet_support(web):
    client, headers = web
    connect(client)
    assert client.post("/api/sessions", json=payload(), headers=headers).status_code == 201
    view = client.get("/api/paper").json()
    assert view["analysis_target"]["market"] == "usdt_perpetual"
    assert view["analysis_target"]["symbol"] == "ETHUSDT"
    assert view["paper_market"] == "spot" and view["market_compatible"] is False
    assert view["account"] is None and not view["enabled"]


def test_chart_selection_is_read_only_and_does_not_change_model_history_or_style(web):
    client, headers = web
    svc = connect(client)
    created = client.post("/api/sessions", json=payload(), headers=headers).json()["session"]
    client.portal.call(svc.step)
    first = client.get("/api/analysis/current").json()["record"]
    response = client.get("/api/analysis/chart?interval=5m&limit=100")
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == created["session_id"]
    assert data["history"]["symbol"] == "ETHUSDT" and data["history"]["interval"] == "5m"
    assert data["history"]["source"] == "fake"
    assert len(data["history"]["candles"]) == 100
    assert client.get("/api/session").json()["session"] == created
    assert client.get("/api/analysis/current").json()["record"] == first
    assert client.get("/api/analysis/chart?interval=1s").status_code == 422
    assert client.get("/api/analysis/chart?interval=5m&limit=10000").status_code == 422
    client.cookies.clear()
    assert client.get("/api/analysis/chart?interval=15m").status_code == 403
