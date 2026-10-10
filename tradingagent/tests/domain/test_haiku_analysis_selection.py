from agent_platform.adapters.openrouter.initial_analysis import MODEL
from agent_platform.domain.background import BackgroundSettings
from agent_platform.domain.model_modules import ModelModulesConfig


def test_new_analysis_defaults_select_haiku55_and_keep_jev_independent():
    modules = ModelModulesConfig()
    assert modules.analysis_model == "anthropic/claude-haiku-5.5"
    assert modules.decision_model == "typesafe/jev-1.13"
    assert not modules.strong_model.calls_enabled
    assert BackgroundSettings().model_id == modules.analysis_model
    assert MODEL == modules.analysis_model


def test_existing_deepseek_selection_still_deserializes():
    previous = ModelModulesConfig.model_validate({"analysis_model": "deepseek/deepseek-v4.1-flash"})
    assert previous.analysis_model == "deepseek/deepseek-v4.1-flash"
