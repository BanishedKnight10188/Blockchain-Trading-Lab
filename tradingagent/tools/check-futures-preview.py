"""Read-only local preview check; never creates sessions, wallets or model calls."""

import json
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, build_opener


def main():
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    root = "http://localhost:8775"
    with client.open(root + "/jev-trader", timeout=10) as response:
        page = response.read(262144).decode()
    with client.open(root + "/api/futures-trading", timeout=10) as response:
        view = json.load(response)
    with client.open(root + "/api/analysis/contracts", timeout=15) as response:
        contracts = json.load(response)["contracts"]
    assert view["enabled"] and view["decision_source"] == "offline_mock"
    assert view["market_source"] == "binance_futures_public"
    assert not view["paid_models_enabled"] and not view["real_orders_enabled"]
    assert view["account"] is None and view["session"] is None
    assert "futures-configure" in page
    assert contracts and any(c["symbol"] == "ETHUSDT" for c in contracts)
    print(
        json.dumps(
            {
                "preview": root,
                "runtime_ready": True,
                "public_contract_count": len(contracts),
                "eth_available": True,
                "market_source": view["market_source"],
                "decision_source": view["decision_source"],
                "model_calls": 0,
                "wallets_created": 0,
                "exchange_orders": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
