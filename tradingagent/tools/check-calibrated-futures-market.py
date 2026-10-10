"""Free runtime-clock and selected-contract probe; no model or wallet is loaded."""

# ruff: noqa: E402 -- standalone workspace tool.
import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_platform.adapters.binance_direct.futures_stream import FuturesStreamMarket
from agent_platform.runtime.clock import CalibratedClock


async def main(args):
    report = {"symbol": args.symbol, "model_calls": 0, "exchange_orders": 0, "samples": []}
    async with CalibratedClock() as clock:
        report["initial_clock"] = clock.public_status
        async with FuturesStreamMarket(clock, proxy_url=args.proxy) as market:
            for _ in range(args.samples):
                try:
                    value = await market.snapshot(args.symbol)
                    row = {
                        "snapshot": value.model_dump(mode="json"),
                        "mark_age_ms": round(
                            (clock.utcnow() - value.quote.mark_at).total_seconds() * 1000, 3
                        ),
                    }
                except Exception as error:
                    row = {"error": type(error).__name__}
                report["samples"].append({**row, "status": market.public_status})
                await asyncio.sleep(1)
            report["final_clock"] = clock.public_status
    report["valid_samples"] = sum("snapshot" in s for s in report["samples"])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = ROOT / "output/verification" / ("calibrated-public-market-" + stamp + ".json")
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "report": str(target),
                "valid_samples": report["valid_samples"],
                "samples": args.samples,
                "clock": report["final_clock"],
                "model_calls": 0,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="ONGUSDT")
    parser.add_argument("--samples", type=int, choices=range(1, 61), default=8)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    asyncio.run(main(parser.parse_args()))
