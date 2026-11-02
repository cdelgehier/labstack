"""Select another recipe without changing state ownership or script paths."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from labstack import cluster, main
from labstack.inventory import read_config, resolve_config
from labstack.models.config import Config
from labstack.settings import get_settings


class UpSpy:
    """Record CLI options without starting infrastructure."""

    def __init__(self):
        self.options: dict[str, object] = {}

    def __call__(self, profiles, image_path, **options):
        self.options = options


class ImageResolutionStub:
    """Stop a real up command at its first remote image lookup."""

    def __init__(self):
        self.config: Config | None = None

    def __call__(self, config):
        self.config = config
        raise RuntimeError("Reached image lookup")


@pytest.mark.parametrize("option", ["-f", "--inventory"])
def test_cli_passes_the_selected_inventory_to_up(tmp_path, monkeypatch, option):
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path / "logs")
    monkeypatch.setattr(sys, "argv", ["labstack", "up", option, "recipes/custom.yml"])
    spy = UpSpy()
    monkeypatch.setattr(main, "up", spy)

    main.main()

    assert spy.options["inventory_path"] == Path("recipes/custom.yml")
    assert spy.options["no_provision"] is False


def test_selected_inventory_does_not_read_the_default_file(tmp_path):
    definition = read_config().model_dump(mode="json")
    definition["machines"]["cp"]["memory_mib"] = 4096
    selected = tmp_path / "machines.yml.custom"
    selected.write_text(yaml.safe_dump(definition))
    (tmp_path / "machines.yml").write_text("invalid: default")

    config = resolve_config(None, None, path=selected)

    assert config.machines["cp"].memory_mib == 4096


def test_selected_inventory_cannot_replace_an_existing_topology(tmp_path):
    existing = read_config()
    definition = existing.model_dump(mode="json")
    definition["machines"]["cp"]["memory_mib"] = 4096
    selected = tmp_path / "machines.yml.custom"
    selected.write_text(yaml.safe_dump(definition))

    with pytest.raises(ValueError, match="task destroy"):
        resolve_config(existing, None, path=selected)


def test_an_identical_inventory_keeps_saved_profiles(tmp_path):
    existing = resolve_config(None, ("extra-worker",))
    selected = tmp_path / "machines.yml.copy"
    selected.write_text(yaml.safe_dump(read_config().model_dump(mode="json")))

    assert resolve_config(existing, None, path=selected) == existing


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("no_provision", [False, True])
def test_up_keeps_project_scripts_and_resolves_selected_env_files(
    tmp_path, monkeypatch, absolute, no_provision
):
    definition = read_config().model_dump(mode="json")
    definition["machines"]["cp"]["env_file"] = ["custom.env"]
    directory = tmp_path / "recipes"
    directory.mkdir()
    (directory / "custom.env").write_text("DEMO_NAME=holmes\n")
    selected = directory / "machines.yml.custom"
    selected.write_text(yaml.safe_dump(definition))
    root = tmp_path / "project"
    root.mkdir()
    for machine in definition["machines"].values():
        for filename in machine["scripts"]:
            script = root / filename
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("exit 0\n")
    monkeypatch.setattr(get_settings(), "root", root)
    monkeypatch.setattr(get_settings(), "state_dir", root / ".state")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cluster, "requirements", Path.cwd)
    monkeypatch.setattr(cluster, "prepare_runtime_directory", Path.cwd)
    stub = ImageResolutionStub()
    monkeypatch.setattr(cluster, "resolve_boxes", stub)

    with pytest.raises(RuntimeError, match="Reached image lookup"):
        cluster.up(
            None,
            None,
            no_provision=no_provision,
            inventory_path=selected if absolute else selected.relative_to(tmp_path),
        )

    assert stub.config is not None
    assert stub.config.machines["cp"].env_file == ("custom.env",)
    assert not (root / ".state").exists()


def test_missing_inventory_fails_before_vm_resources_are_created(tmp_path):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("labstack")), "up", "-f", "missing.yml"],
        cwd=tmp_path,
        env=dict(os.environ, LABSTACK_ROOT=str(tmp_path)),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "missing.yml" in json.loads(result.stderr)["error"]
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize(
    "action",
    [
        "down",
        "destroy",
        "reset",
        "status",
        "ssh",
        "exec",
        "kubeconfig",
        "test",
        "client",
        "kubectl",
    ],
)
def test_other_commands_do_not_accept_an_inventory_selector(tmp_path, action):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("labstack")), action, "-f", "another.yml"],
        cwd=tmp_path,
        env=dict(os.environ, LABSTACK_ROOT=str(tmp_path)),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "only available with up" in result.stderr
    assert not (tmp_path / ".cluster.lock").exists()
