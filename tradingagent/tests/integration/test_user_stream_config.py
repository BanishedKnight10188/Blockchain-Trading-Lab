"""Private subscription is an independent explicit opt-in, never a default credential use."""

import builtins

import pytest

from agent_platform.config import RuntimeConfig


def test_user_stream_requires_read_only_account_opt_in():
    assert RuntimeConfig().live_user_stream is False
    with pytest.raises(ValueError):
        RuntimeConfig(live_user_stream=True)
    assert RuntimeConfig(live_account=True, live_user_stream=True).live_user_stream is True
    with pytest.raises(ValueError):
        RuntimeConfig(live_account=True, live_user_stream=1)


@pytest.mark.asyncio
async def test_missing_private_stream_dependency_is_fixed_configuration_error(
    tmp_path, monkeypatch
):
    import agent_platform.adapters.binance_direct.read_client as reader
    import agent_platform.bootstrap as bootstrap
    from agent_platform.config import RuntimeConfigurationError

    closed = []

    class FakeReader:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "agent_platform.adapters.binance_direct.user_stream":
            raise ImportError("private-dependency-path")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(bootstrap, "load_credentials", lambda config: object())
    monkeypatch.setattr(reader, "ReadOnlyClient", FakeReader)
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeConfigurationError, match="私流"):
        async with bootstrap.build_application_services(
            tmp_path / "missing.sqlite3", RuntimeConfig(live_account=True, live_user_stream=True)
        ):
            pytest.fail("missing stream dependency must reject before workers")
    assert closed == [True]
