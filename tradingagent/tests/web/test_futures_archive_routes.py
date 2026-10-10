"""Archives and full exports retain the same local browser authentication."""

from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app
from tests.web.test_jev_parallel_controls import headers


def test_archive_routes_require_cookie_and_a_selected_futures_wallet(tmp_path):
    app = create_app(
        tmp_path / "archive-web.sqlite3",
        runtime_config=RuntimeConfig(paper=True, paper_mock=True, market_archive=False),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/futures-trading/archive").status_code == 403
        assert client.get("/api/futures-trading/archive/export").status_code == 403
        headers(client)
        assert client.get("/api/futures-trading/archive").status_code == 409
        assert client.get("/api/futures-trading/archive?after=-1").status_code == 422
        assert client.get("/api/futures-trading/archive/export?format=html").status_code == 422
        page = client.get("/jev-trader")
        assert "逐笔合约档案" in page.text
        assert "完整 JSONL" in page.text and "CSV 汇总" in page.text
