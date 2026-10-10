"""Bounded free public probe. No account credentials, paid model or wallet access."""

# ruff: noqa: E402 -- standalone workspace tool
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient
from agent_platform.adapters.binance_direct.multiscale import NativeFuturesKlines
from agent_platform.domain.multiscale import BACKGROUND_WINDOWS
from agent_platform.runtime.clock import CalibratedClock


async def run(args):
    clock = CalibratedClock()
    await clock.refresh()
    async with FuturesPublicClient(clock, proxy_url=args.proxy) as public:
        feed = NativeFuturesKlines(clock, public=public, proxy_url=args.proxy)
        try:
            await feed.start(args.symbol)
            for tick in range(args.seconds):
                await asyncio.sleep(1)
                if tick % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "elapsed_seconds": tick + 1,
                                "connected": feed.connected,
                                "failure": feed.failure,
                                "seconds_count": len(
                                    feed.buffer.window("1s", 60, clock.utcnow()).candles
                                ),
                            }
                        ),
                        flush=True,
                    )
            windows = [
                feed.buffer.window(i, n, clock.utcnow())
                for i, n in (*BACKGROUND_WINDOWS, ("3m", 20), ("1s", 60))
            ]
            report = {
                "symbol": args.symbol,
                "clock": clock.public_status,
                "connected": feed.connected,
                "failure": feed.failure,
                "rejected_frames": feed.rejected_frames,
                "ready": all(w.complete and w.fresh for w in windows),
                "windows": [w.evidence() for w in windows],
                "short_3m": windows[-2].model_dump(mode="json"),
                "fast_1s": windows[-1].model_dump(mode="json"),
                "paid_calls": 0,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {"report": str(args.output), "ready": report["ready"], "failure": feed.failure}
                ),
                flush=True,
            )
        finally:
            await feed.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--seconds", type=int, choices=range(5, 71), default=65)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
