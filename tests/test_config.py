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


def test_settings_of_the_wrong_type_are_refused_by_name():
    with pytest.raises(ValueError, match="context_window_limit in job.toml must be a whole number"):
        HarnessConfig.load(None, job={"context_window_limit": [1]}, user_config=None)
    with pytest.raises(ValueError, match="model_id in job.toml must be a string"):
        HarnessConfig.load(None, job={"model_id": 3}, user_config=None)
    with pytest.raises(ValueError, match="egress_allow in job.toml must be a list"):
        HarnessConfig.load(None, job={"egress_allow": 3}, user_config=None)


def test_quoted_numbers_and_flags_in_toml_are_read_as_their_type():
    cfg = HarnessConfig.load(None, job={"context_window_limit": "65536", "thinking": "false"}, user_config=None)
    assert cfg.context_window_limit == 65536 and cfg.thinking is False
    with pytest.raises(ValueError, match="context_window_limit"):
        HarnessConfig.load(None, job={"context_window_limit": "lots"}, user_config=None)


def test_the_default_allowlist_lets_package_managers_fetch():
    allow = HarnessConfig().egress_allow
    for host in ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                 "crates.io", "index.crates.io", "static.crates.io",
                 "docs.rs", "static.docs.rs", "codeload.github.com"):  # Rust's docs, a tag's source archive
        assert host in allow
