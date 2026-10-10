import importlib
import sys

import pytest

from agent_platform.config import RuntimeConfig


def models():
    return importlib.import_module("agent_platform.domain.model_modules")


def test_selection_is_serializable_and_cannot_enable_strong_calls():
    config = models().ModelModulesConfig(strong_model={"model_id": "vendor/reviewer-v1"})
    assert config.analysis_model == "anthropic/claude-haiku-5.5"
    assert config.decision_model == "typesafe/jev-1.13"
    assert config.strong_model.model_id == "vendor/reviewer-v1"
    assert config.strong_model.calls_enabled is False
    assert models().ModelModulesConfig.model_validate_json(config.model_dump_json()) == config
    with pytest.raises(ValueError):
        models().StrongModelSelection(model_id="vendor/reviewer-v1", calls_enabled=True)
    with pytest.raises(ValueError):
        config.strong_model.model_id = "vendor/other"


@pytest.mark.parametrize(
    "value", ["https://evil/model", "model", "x/y?token=secret", "a/b\n", "a/" + "x" * 128]
)
def test_invalid_model_ids_cannot_be_endpoint_or_credential_containers(value):
    with pytest.raises(ValueError):
        models().StrongModelSelection(model_id=value)


def test_model_module_config_cannot_store_credentials_or_substitute_jev_router():
    with pytest.raises(ValueError):
        models().ModelModulesConfig(api_key="never-export")
    with pytest.raises(ValueError):
        models().ModelModulesConfig(decision_model="typesafe/jev-router")


def test_strong_model_cannot_be_dispatched_by_relabeling_as_analysis():
    with pytest.raises(ValueError):
        models().ModelModulesConfig(
            analysis_model="anthropic/claude-opus-5.5",
            strong_model={"model_id": "anthropic/claude-opus-5.5"},
        )
    with pytest.raises(ValueError):
        models().ModelModulesConfig(strong_model={"model_id": "deepseek/deepseek-v4.1-flash"})


@pytest.mark.asyncio
async def test_selected_strong_module_does_not_enable_or_assemble_paid_models(tmp_path):
    from agent_platform.bootstrap import build_application_services

    config = RuntimeConfig(model_modules={"strong_model": {"model_id": "vendor/reviewer-v1"}})
    async with build_application_services(tmp_path / "agent.sqlite3", config) as services:
        state = await services.system.current()
        assert state["model_modules"]["strong_model"] == {
            "model_id": "vendor/reviewer-v1",
            "calls_enabled": False,
        }
        assert state["paid_models_enabled"] is False
        assert state["routing"]["daily_limit_usd"] == "0"


def test_cli_strong_selection_changes_only_configuration(monkeypatch):
    from agent_platform import cli

    captured = []
    monkeypatch.setattr(sys, "argv", ["btc-agent", "web", "--strong-model", "vendor/reviewer-v1"])
    monkeypatch.setattr(
        cli, "create_app", lambda path, runtime_config: captured.append(runtime_config)
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    cli.main()
    assert captured[0].model_modules.strong_model.model_id == "vendor/reviewer-v1"
    assert captured[0].model_modules.strong_model.calls_enabled is False


def test_jev_setting_is_independent_disabled_by_default_and_roundtrips():
    config = models().ModelModulesConfig()
    assert config.jev.enabled is False
    assert config.jev.mode == "independent_position"
    assert config.jev.account_provider == "binance_agent_os"
    enabled = models().ModelModulesConfig(jev={"enabled": True})
    assert models().ModelModulesConfig.model_validate_json(enabled.model_dump_json()) == enabled
    assert enabled.analysis_model == config.analysis_model
    assert enabled.strong_model.calls_enabled is False
    with pytest.raises(ValueError):
        enabled.jev.enabled = False


@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_jev_switch_does_not_accept_coercion(value):
    setting = models().JevModuleSettings
    assert setting(enabled=True).enabled is True
    with pytest.raises(ValueError):
        setting(enabled=value)


@pytest.mark.parametrize(
    "extra", [{"mode": "cascade"}, {"account_provider": "main-account"}, {"api_key": "secret"}]
)
def test_jev_configuration_cannot_change_its_execution_scope(extra):
    setting = models().JevModuleSettings
    assert setting().enabled is False
    with pytest.raises(ValueError):
        setting(**extra)


def test_two_operating_modes_roundtrip_without_a_live_execution_option():
    setting = importlib.import_module("agent_platform.domain.operating_modes").OperatingSettings
    assert setting().mode == "advisory"
    auto = setting(mode="auto")
    assert auto.execution_environment == "testnet"
    assert setting.model_validate_json(auto.model_dump_json()) == auto
    paper = setting(mode="auto", execution_environment="paper")
    assert setting.model_validate_json(paper.model_dump_json()) == paper
    for invalid in (
        {"mode": "live"},
        {"mode": "AUTO"},
        {"execution_environment": "production"},
        {"writes_enabled": True},
    ):
        with pytest.raises(ValueError):
            setting(**invalid)


@pytest.mark.parametrize("field", ["live_public", "live_account", "live_user_stream"])
def test_auto_testnet_cannot_mix_production_read_switches(field):
    assert RuntimeConfig(operation={"mode": "auto"}).operation.mode == "auto"
    switches = {field: True}
    if field == "live_user_stream":
        switches["live_account"] = True
    with pytest.raises(ValueError):
        RuntimeConfig(operation={"mode": "auto"}, **switches)


@pytest.mark.parametrize(
    "mode,enabled,trader_enabled,state",
    [
        ("advisory", False, False, "advisory_only"),
        ("advisory", True, True, "advisory_only"),
        ("auto", False, False, "trader_disabled"),
        ("auto", True, False, "trader_disabled"),
        ("auto", False, True, "execution_unconfigured"),
        ("auto", True, True, "execution_unconfigured"),
    ],
)
@pytest.mark.asyncio
async def test_mode_selection_and_jev_enablement_do_not_create_execution_or_paid_clients(
    tmp_path, mode, enabled, trader_enabled, state
):
    from agent_platform.bootstrap import build_application_services

    config = RuntimeConfig(
        operation={"mode": mode},
        model_modules={
            "jev": {"enabled": enabled},
            "jev_trader": {"enabled": trader_enabled},
        },
    )
    async with build_application_services(tmp_path / "agent.sqlite3", config) as services:
        result = await services.system.current()
        assert result["operation"] == {
            "mode": mode,
            "execution_environment": "testnet",
            "decision_authority": "jev",
            "writes_enabled": False,
            "state": state,
        }
        assert result["model_modules"]["jev"]["enabled"] is enabled
        assert result["model_modules"]["jev_trader"]["enabled"] is trader_enabled
        assert result["model_modules"]["analysis_mode"] == "continuous"
        assert result["model_modules"]["jev"]["mode"] == "independent_position"
        assert result["paid_models_enabled"] is False
        assert result["routing"]["daily_limit_usd"] == "0"
        assert result["routing"]["configured_routes"] == 0


@pytest.mark.parametrize("flag,enabled", [("--jev", True), ("--no-jev", False)])
def test_cli_can_select_mode_and_jev_without_enabling_calls(monkeypatch, flag, enabled):
    from agent_platform import cli

    captured = []
    monkeypatch.setattr(sys, "argv", ["btc-agent", "web", "--mode", "auto", flag])
    monkeypatch.setattr(
        cli, "create_app", lambda path, runtime_config: captured.append(runtime_config)
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    cli.main()
    assert captured[0].operation.mode == "auto"
    assert captured[0].model_modules.jev.enabled is enabled
