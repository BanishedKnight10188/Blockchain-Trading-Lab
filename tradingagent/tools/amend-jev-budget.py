"""Append a user-confirmed cumulative ceiling; never reset fees or old policies."""
# ruff: noqa: E402 -- workspace command.

import argparse
import asyncio
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "multiscale_trial", ROOT / "tools/run-multiscale-trial.py"
)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)

from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.paper import SqlitePaperStore
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.runtime.clock import SystemClock


async def run(args):
    if not args.confirm:
        raise ValueError("explicit_budget_confirmation_required")
    config = ROOT / "data/jev-paper-continuous.local.json"
    old_raw = config.read_bytes()
    old = PaperTrialPolicy.model_validate_json(old_raw)
    before = trial.preservation_snapshot()
    core = await open_store(trial.trial.SHARED_BUDGET)
    states = SqlitePaperStore(core.path)
    saved = await states.load(old.budget_key)
    if saved is None or saved.state.policy != old:
        raise ValueError("active_configuration_differs_from_ledger")
    now = SystemClock().utcnow()
    prior = await core.budget_balance(now, daily_limit_usd=old.trial_total_usd, cumulative=True)
    if prior.daily_limit_usd != old.trial_total_usd:
        raise ValueError("active_policy_has_been_superseded")
    if old.trial_total_usd == args.total_usd:
        return {"unchanged": True, "policy": old.model_dump(mode="json")}
    amendment = PaperTrialPolicy.model_validate(
        old.model_dump()
        | {
            "grant_id": "confirmed-usd1-20261009-" + uuid4().hex[:8],
            "trial_total_usd": args.total_usd,
            "issued_at": now,
            "supersedes_budget_key": old.budget_key,
            "budget_change_confirmed": True,
        }
    )
    backup = args.report.with_suffix(".original-config.json")
    with backup.open("xb") as stream:
        stream.write(old_raw)
    await states.ensure_trial(amendment, now)
    pending = args.report.with_suffix(".pending-config.json")
    with pending.open("x", encoding="utf-8") as stream:
        stream.write(amendment.model_dump_json(indent=2) + "\n")
    pending.replace(config)
    after = trial.preservation_snapshot()
    preserved = True
    for name, rows in before.items():
        if name == "data/jev-paper-continuous.local.json":
            continue  # Original exact bytes retained above; only current pointer changes.
        current = after[name]
        preserved &= all(current.get(key) == value for key, value in rows.items())
        additions = set(current) - set(rows)
        expected = (
            {amendment.budget_key}
            if name == ("data\\futures-core-preview-20261008.sqlite3:domain_states")
            else set()
        )
        preserved &= additions == expected
    balance = await core.budget_balance(now, daily_limit_usd=args.total_usd, cumulative=True)
    assert balance.spent_usd == prior.spent_usd and balance.reserved_usd == prior.reserved_usd
    assert balance.daily_limit_usd == args.total_usd and preserved
    return {
        "user_authorization": "2026-10-09 cumulative model limit reset to 1 USD; continue workflow",
        "policy": amendment.model_dump(mode="json"),
        "parent_policy": old.model_dump(mode="json"),
        "before": prior.model_dump(mode="json"),
        "after": balance.model_dump(mode="json"),
        "old_rows_preserved": preserved,
        "original_config_backup": str(backup),
        "paid_calls": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--total-usd", type=Decimal, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write('{"state":"started"}\n')
    report = asyncio.run(run(args))
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(report, ensure_ascii=False))
