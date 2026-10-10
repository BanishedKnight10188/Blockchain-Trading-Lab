import importlib.util
from pathlib import Path

import pytest

from agent_platform.ports.model import ModelCallFailed


def load_tool():
    path = Path(__file__).parents[2] / "tools/run-multiscale-trial.py"
    spec = importlib.util.spec_from_file_location("multiscale_trial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_one_background_dispatch_is_never_repeated_after_failure():
    module = load_tool()
    calls = []

    class Model:
        async def analyze(self, request):
            calls.append(request)
            raise ModelCallFailed("provider_timeout")

    once = module.OnceBackground(Model())
    with pytest.raises(ModelCallFailed):
        await once.analyze("first")
    with pytest.raises(ModelCallFailed):
        await once.analyze("second")
    assert calls == ["first"]


def test_preservation_allows_new_fees_but_never_changes_original_rows():
    module = load_tool()
    before = {"wallet:sessions": {"s1": "unchanged"}, "shared:budget_requests": {"r1": "held"}}
    after = {**before, "shared:budget_requests": {"r1": "held", "r2": "new"}}
    assert module.preserved(before, after)
    after["shared:budget_requests"]["r1"] = "released"
    assert not module.preserved(before, after)


def test_joint_bound_counts_flash_and_jev_without_raising_allowance():
    from decimal import Decimal

    module = load_tool()
    assert module.joint_bound(10000, Decimal("0.03"), Decimal("0.4"), Decimal("0.042"), 0) == (
        Decimal("0.0024632")
    )
