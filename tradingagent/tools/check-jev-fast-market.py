"""Free public-stream probe. No credentials, model dispatch or wallet mutation."""

# ruff: noqa: E402 -- standalone workspace tool.
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_platform.adapters.binance_direct.futures_stream import FuturesStreamMarket
from agent_platform.runtime.clock import SystemClock


async def main():
    clock = SystemClock()
    report = {"symbol": "BTCUSDT", "model_calls": 0, "exchange_orders": 0, "samples": []}
    async with FuturesStreamMarket(clock, proxy_url="http://127.0.0.1:7897") as market:
        for _ in range(8):
            try:
                snapshot = await market.snapshot("BTCUSDT")
                report["samples"].append(
                    {
                        "at": clock.utcnow().isoformat(),
                        "quote": snapshot.quote.model_dump(mode="json"),
                        "recent_count": len(snapshot.recent_quotes),
                        "age_ms": round(
                            (clock.utcnow() - snapshot.quote.mark_at).total_seconds() * 1000, 1
                        ),
                    }
                )
            except Exception as error:
                report["samples"].append(
                    {
                        "at": clock.utcnow().isoformat(),
                        "failure": type(error).__name__,
                        "stream_failure": market.last_failure,
                    }
                )
            await asyncio.sleep(1)
    report["valid_samples"] = sum("quote" in item for item in report["samples"])
    target = ROOT / "output/verification/jev-fast-public-stream-20261008.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(target),
                "valid_samples": report["valid_samples"],
                "samples": len(report["samples"]),
                "model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
