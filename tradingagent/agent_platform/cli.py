"""Explicit loopback-only local Web launcher."""

import argparse
import asyncio
import json
from dataclasses import asdict
from itertools import chain
from pathlib import Path

import uvicorn

from agent_platform.bootstrap import load_credentials
from agent_platform.config import RuntimeConfig, RuntimeConfigurationError
from agent_platform.web.app import create_app


async def _replay(arguments: argparse.Namespace) -> None:
    from agent_platform.adapters.rules.baseline import RuleAdvisoryProvider
    from agent_platform.domain.account import AccountSnapshot
    from agent_platform.domain.sessions import AgentSession, TradingStyle
    from agent_platform.replay.reader import read_events
    from agent_platform.replay.runner import ReplayRunner

    events = read_events(arguments.input)
    try:
        first = next(events)
    except StopIteration:
        raise ValueError("replay input contains no events") from None
    started = min(first.occurred_at, first.received_at)
    runner = ReplayRunner(
        AgentSession(
            session_id="offline-session",
            style=TradingStyle(strength=arguments.style),
            created_at=started,
            updated_at=started,
        ),
        AccountSnapshot(account_ref="offline-observation", as_of=started, status="unavailable"),
    )
    report = await runner.run(chain((first,), events), RuleAdvisoryProvider(arguments.threshold))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as stream:
        stream.write(report.model_dump_json(indent=2) + "\n")
    print(
        f"Offline replay: {report.events_processed} events; "
        f"network={report.network_calls}, real orders={report.real_orders}. "
        f"Report: {arguments.output}"
    )


async def _market_probe(arguments: argparse.Namespace) -> None:
    from agent_platform.adapters.binance_direct.market_stream import BinanceMarketStream
    from agent_platform.adapters.binance_direct.public_rest import PublicRestClient, PublicRestError
    from agent_platform.runtime.clock import SystemClock
    from agent_platform.runtime.market_probe import capture_market

    clock = SystemClock()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the exact output before networking; never replace an existing result.
    with arguments.output.open("x", encoding="utf-8") as target:
        async with PublicRestClient(clock) as rest:
            client = BinanceMarketStream(rest, clock)
            try:
                server_at = await rest.server_time()
                skew = abs((clock.utcnow() - server_at).total_seconds())
                if skew > 5:
                    raise ValueError(
                        "public probe clock differs from server by more than 5 seconds"
                    )
                report = await capture_market(client, seconds=arguments.seconds)
                report["clock_difference_seconds"] = skew
            except (PublicRestError, ValueError):
                report = {
                    "schema_version": 1,
                    "mode": "public_market_probe",
                    "status": "blocked",
                    "error": "public endpoint or clock verification failed",
                    "events_received": 0,
                    "snapshot": None,
                    "features": None,
                    "account_calls": 0,
                    "real_orders": 0,
                    "model_calls": 0,
                }
            report["transport"] = asdict(client.stats)
            target.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Public market probe: {report['status']}; report: {arguments.output}")
    if report["status"] != "captured":
        raise ValueError("public probe did not capture usable evidence")


def _runtime_config(arguments):
    return RuntimeConfig(
        live_public=arguments.live_public,
        futures_proxy=getattr(arguments, "futures_proxy", None),
        openrouter_proxy=getattr(arguments, "openrouter_proxy", None),
        live_account=arguments.live_account,
        live_user_stream=arguments.live_user_stream,
        market_archive=not arguments.no_market_archive,
        model_modules={
            "strong_model": {"model_id": arguments.strong_model},
            "jev": {"enabled": arguments.jev},
            "jev_trader": {"enabled": arguments.jev_trader},
        },
        operation={
            "mode": arguments.mode,
            "execution_environment": getattr(arguments, "trader_environment", "testnet"),
        },
        paper=getattr(arguments, "paper", False),
        paper_mock=getattr(arguments, "paper_mock", False),
        paper_read_only=getattr(arguments, "paper_read_only", False),
        paper_model_config=getattr(arguments, "paper_model_config", None),
        model_budget_database=getattr(arguments, "model_budget_database", None),
        futures_multiscale=getattr(arguments, "futures_multiscale", False),
        background_model_config=getattr(arguments, "background_model_config", None),
        market_retention={
            "raw_days": arguments.raw_retention_days,
            "minute_days": arguments.minute_retention_days,
        },
    )


async def _soak(arguments):
    from agent_platform.runtime.soak import capture_soak

    config = _runtime_config(arguments)
    load_credentials(config)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as target:
        report = await capture_soak(arguments.database, config, seconds=arguments.seconds)
        target.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Run report: {report['verdict']}; formal_acceptance=false; output: {arguments.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance BTC local advisory assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Verified SQLite online copy; no network")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--kind", choices=("core", "market"), required=True)
    soak = commands.add_parser("soak", help="Measured run; default offline short check")
    soak.add_argument("--database", type=Path, required=True)
    soak.add_argument("--output", type=Path, required=True)
    soak.add_argument("--seconds", type=int, default=60, metavar="1-604800")
    soak.add_argument("--live-public", action="store_true")
    soak.add_argument("--live-account", action="store_true")
    soak.add_argument("--live-user-stream", action="store_true")
    soak.add_argument("--no-market-archive", action="store_true")
    replay = commands.add_parser("replay", help="Run an offline M0 test rule; no real orders")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--style", type=int, required=True, choices=range(101), metavar="0-100")
    replay.add_argument("--threshold", required=True, help="Explicit test rule price threshold")
    probe = commands.add_parser("market-probe", help="Explicit public-only transport diagnostic")
    probe.add_argument("--live-public", action="store_true", help="Opt in to public network reads")
    probe.add_argument("--seconds", type=int, choices=range(1, 61), default=10, metavar="1-60")
    probe.add_argument("--output", type=Path, required=True)
    web = commands.add_parser("web", help="Open local settings and read-only overview")
    web.add_argument("--live-public", action="store_true", help="Enable public market reads")
    web.add_argument(
        "--futures-proxy",
        help="Optional credential-free loopback HTTP proxy for futures public reads",
    )
    web.add_argument(
        "--openrouter-proxy",
        help="Optional credential-free loopback HTTP proxy for paid OpenRouter requests",
    )
    web.add_argument(
        "--model-budget-database",
        type=Path,
        help="Existing shared model-fee database, independent from virtual wallet storage",
    )
    web.add_argument(
        "--live-account", action="store_true", help="Enable signed read-only account polling"
    )
    web.add_argument(
        "--live-user-stream",
        action="store_true",
        help="Opt in to private reconciliation hints; requires --live-account",
    )
    web.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    web.add_argument(
        "--no-market-archive",
        action="store_true",
        help="Disable auxiliary public samples/minute history; permanent audit is retained",
    )
    web.add_argument("--port", type=int, default=8765)
    web.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "agent.sqlite3",
    )
    defaults = RuntimeConfig().market_retention
    for command in (web, soak):
        command.add_argument(
            "--paper", action="store_true", help="Enable the independent local virtual-funds worker"
        )
        paper_source = command.add_mutually_exclusive_group()
        command.add_argument(
            "--paper-read-only",
            action="store_true",
            help="Load an existing real Paper trial without paid dispatch or renewal",
        )
        paper_source.add_argument(
            "--paper-mock",
            action="store_true",
            help="Explicit scripted offline decisions, zero model calls",
        )
        paper_source.add_argument(
            "--paper-model-config",
            type=Path,
            help="Local real JEV trial caps and verified price JSON; key stays in environment",
        )
        command.add_argument(
            "--futures-multiscale",
            action="store_true",
            help="Use four background layers plus native 3m/1s windows",
        )
        command.add_argument(
            "--background-model-config",
            type=Path,
            help="Local background model and verified price JSON; shares existing fee caps",
        )
        command.add_argument(
            "--trader-environment", choices=("paper", "testnet"), default="testnet"
        )
        command.add_argument(
            "--jev-trader",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="Select the separate Jev trading module; does not enable an executor",
        )
        command.add_argument(
            "--mode",
            choices=("advisory", "auto"),
            default="advisory",
            help="Jev trader mode; testnet auto stays blocked until its executor is configured",
        )
        command.add_argument(
            "--jev",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="Optional Jev advice selection; independent from --jev-trader",
        )
        command.add_argument(
            "--strong-model",
            default=None,
            metavar="PROVIDER/MODEL",
            help="Select an optional review model; calls remain disabled",
        )
        command.add_argument(
            "--raw-retention-days",
            type=int,
            default=defaults.raw_days,
            metavar="1-365",
            help="Auxiliary quote-sample retention in days; permanent audit/pins retained",
        )
        command.add_argument(
            "--minute-retention-days",
            type=int,
            default=defaults.minute_days,
            metavar="1-365",
            help="Auxiliary completed-minute retention in days; permanent audit/pins retained",
        )
    arguments = parser.parse_args()
    if arguments.command == "soak":
        from agent_platform.ports.sessions import PersistenceUnavailable

        if not 1 <= arguments.seconds <= 604800:
            parser.error("soak seconds must be 1–604800")
        try:
            asyncio.run(_soak(arguments))
        except (ValueError, OSError, PersistenceUnavailable):
            parser.error("run capture failed; check local config/source and use a new report path")
        return
    if arguments.command == "backup":
        from agent_platform.adapters.sqlite.backup import backup_database
        from agent_platform.ports.sessions import PersistenceUnavailable

        try:
            result = asyncio.run(
                backup_database(
                    arguments.database,
                    arguments.output,
                    kind=arguments.kind,
                )
            )
        except (ValueError, OSError, PersistenceUnavailable):
            parser.error("online backup failed; check source/schema and choose a new output file")
        print(json.dumps(result, ensure_ascii=False))
        return
    if arguments.command == "market-probe":
        if not arguments.live_public:
            parser.error("market-probe requires --live-public")
        if arguments.output.exists():
            parser.error("market-probe output already exists; choose a new file")
        try:
            asyncio.run(_market_probe(arguments))
        except (ValueError, OSError):
            parser.error(
                "public market probe failed; inspect its report and network/clock settings"
            )
        return
    if arguments.command == "replay":
        from agent_platform.replay.reader import ReplayInputError

        if arguments.output.exists():
            parser.error("replay output already exists; choose a new file")
        try:
            asyncio.run(_replay(arguments))
        except ReplayInputError as failure:
            parser.error(str(failure))
        except (ValueError, OSError):
            parser.error("offline replay failed; check the input and output configuration")
        return
    try:
        config = _runtime_config(arguments)
        load_credentials(config)
    except RuntimeConfigurationError as error:
        parser.error(str(error))
    except ValueError:
        parser.error("运行配置无效；保留时长须为1–365整数，私流须同时启用账户只读。")
    uvicorn.run(
        create_app(arguments.database, runtime_config=config),
        host=arguments.host,
        port=arguments.port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
