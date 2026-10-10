"""Explicit network switches and credentials remain in the assembly boundary."""

import importlib
import os
import subprocess
import sys

import pytest


def module():
    return importlib.import_module("agent_platform.bootstrap")


def test_network_default_is_off_and_does_not_read_environment():
    config = module().RuntimeConfig()

    class Denied(dict):
        def get(self, name, default=None):
            raise AssertionError("disabled mode must not read credentials")

    assert not config.live_public and not config.live_account
    assert module().load_credentials(config, Denied()) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"live_public": 1},
        {"live_account": "true"},
        {"api_secret": "never-echo"},
        {"account_ref": "paper:wrong-scope"},
        {"symbol": "ETHUSDT"},
    ],
)
def test_invalid_runtime_configuration_is_rejected(payload):
    with pytest.raises(ValueError):
        module().RuntimeConfig(**payload)


@pytest.mark.parametrize(
    "environment", [{}, {"BINANCE_API_KEY": "never-echo"}, {"BINANCE_API_SECRET": "never-echo"}]
)
def test_account_switch_requires_complete_credentials_without_echo(environment):
    with pytest.raises(module().RuntimeConfigurationError) as error:
        module().load_credentials(module().RuntimeConfig(live_account=True), environment)
    assert "never-echo" not in str(error.value)


def test_credentials_are_not_a_runtime_config_or_browser_field():
    config = module().RuntimeConfig(live_account=True)
    credentials = module().load_credentials(
        config,
        {
            "BINANCE_API_KEY": "test-key-never-export",
            "BINANCE_API_SECRET": "test-secret-never-export",
        },
    )
    assert "never-export" not in repr(credentials)
    assert "never-export" not in config.model_dump_json()


def test_disabled_web_and_replay_import_without_optional_market_packages():
    script = """
import importlib.abc, sys
class DenyMarket(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'httpx', 'websockets'}:
            raise ModuleNotFoundError('optional market package unavailable')
sys.meta_path.insert(0, DenyMarket())
from agent_platform.web.app import create_app
from agent_platform.cli import main
from agent_platform.bootstrap import load_credentials, RuntimeConfig
assert load_credentials(RuntimeConfig()) is None
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cli_missing_account_credentials_fails_before_server_or_database(tmp_path):
    environment = dict(os.environ)
    environment.pop("BINANCE_API_KEY", None)
    environment.pop("BINANCE_API_SECRET", None)
    target = tmp_path / "not-created.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_platform.cli",
            "web",
            "--live-account",
            "--database",
            str(target),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "BINANCE_API_KEY" in result.stderr
    assert not target.exists()
