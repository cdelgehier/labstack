"""Check command settings without touching the user's VM directories."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from labstack.settings import Settings


def test_working_paths_stay_under_the_project_root(tmp_path):
    settings = Settings(root=tmp_path)

    assert settings.state_dir == tmp_path / ".state"
    assert settings.cache_dir == tmp_path / ".cache"
    assert settings.logs_dir == tmp_path / "logs"


def test_task_environment_values_are_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("PROFILES", "extra-worker,monitoring")
    monkeypatch.setenv("IMAGE_PATH", str(tmp_path / "ubuntu.qcow2"))
    monkeypatch.setenv("KUBECTL_ARGS", "get pods -A")
    monkeypatch.setenv("LABSTACK_LOG_LEVEL", "debug")

    settings = Settings()

    assert settings.profiles == ("extra-worker", "monitoring")
    assert settings.image_path == tmp_path / "ubuntu.qcow2"
    assert settings.kubectl_args == "get pods -A"
    assert settings.log_level == "debug"


def test_empty_task_defaults_keep_optional_values_unset(monkeypatch):
    monkeypatch.setenv("PROFILES", "")
    monkeypatch.setenv("IMAGE_PATH", "")
    monkeypatch.setenv("KUBECTL_ARGS", "")

    settings = Settings()

    assert settings.profiles is None
    assert settings.image_path is None
    assert settings.kubectl_args == "get nodes -o wide"


@pytest.mark.parametrize(
    "profiles", ["../extra-worker", "extra worker", "extra-worker,,monitoring"]
)
def test_invalid_task_profiles_are_rejected(monkeypatch, profiles):
    monkeypatch.setenv("PROFILES", profiles)

    with pytest.raises(ValidationError):
        Settings()


def test_absolute_cache_path_is_kept(tmp_path):
    settings = Settings(root=tmp_path / "project", cache_dir=tmp_path / "cache")

    assert settings.cache_dir == tmp_path / "cache"
    assert isinstance(settings.root, Path)


@pytest.mark.parametrize("state_dir", [".", "provision", "../shared-state"])
def test_state_paths_cannot_redirect_environment_cleanup(tmp_path, state_dir):
    with pytest.raises(ValidationError, match="State directory"):
        Settings(root=tmp_path, state_dir=state_dir)


def test_state_symlinks_cannot_redirect_environment_cleanup(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError, match="State directory"):
        Settings(root=project)


@pytest.mark.parametrize("field", ["cache_dir", "logs_dir"])
def test_kept_data_cannot_be_placed_inside_the_removed_state_directory(tmp_path, field):
    with pytest.raises(ValidationError, match="outside the state directory"):
        Settings.model_validate({"root": tmp_path, field: f".state/{field}"})
