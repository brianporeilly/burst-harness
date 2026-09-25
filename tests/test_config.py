import pytest

from harness.config import REPO_SETTABLE, HarnessConfig


def test_repo_file_cannot_set_driver_settings():
    for key in ("token_command", "base_url", "kubectl", "target", "state_dir", "provider"):
        with pytest.raises(ValueError, match="may not set"):
            HarnessConfig.load(f'{key} = "x"\n', user_config=None)


def test_repo_file_sets_workflow_settings():
    cfg = HarnessConfig.load('base_branch = "develop"\nprotected_branches = ["develop"]\n', user_config=None)
    assert cfg.base_branch == "develop"
    assert cfg.protected_branches == ("develop",)


def test_layering_order(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    user.write_text('pool = "from-user"\nbase_branch = "from-user"\neffort = "low"\n')
    monkeypatch.setenv("HARNESS_EFFORT", "medium")
    cfg = HarnessConfig.load('base_branch = "from-repo"\n', {"pool": "from-cli"}, user_config=user)
    assert (cfg.pool, cfg.base_branch, cfg.effort) == ("from-cli", "from-repo", "medium")


def test_allowlist_names_real_fields():
    assert REPO_SETTABLE <= set(HarnessConfig.__dataclass_fields__)


def test_unknown_key_is_an_error(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text('poool = "typo"\n')
    with pytest.raises(ValueError, match="Unknown setting"):
        HarnessConfig.load(None, user_config=user)
