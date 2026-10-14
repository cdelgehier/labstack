"""Check that the package can be started without creating VM resources."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_entrypoints_list_the_existing_commands_from_another_directory(
    tmp_path, entrypoint
):
    command = (
        [sys.executable, "-m", "labstack"]
        if entrypoint == "module"
        else [str(Path(sys.executable).with_name("labstack"))]
    )
    result = subprocess.run(
        [*command, "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: labstack" in result.stdout
    assert "--no-provision" in result.stdout
    assert "kubeconfig" in result.stdout
    assert "reset" in result.stdout


def test_invalid_task_settings_produce_one_error_event_and_a_failure_exit(tmp_path):
    environment = dict(
        os.environ, PROFILES="../extra-worker", LABSTACK_ROOT=str(tmp_path)
    )

    result = subprocess.run(
        [str(Path(sys.executable).with_name("labstack")), "up"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stderr)["level"] == "error"
    assert "PROFILES" in json.loads(result.stderr)["error"]
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize("action", ["ssh", "exec"])
def test_guest_commands_require_an_explicit_machine_name(tmp_path, action):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("labstack")), action],
        env=dict(os.environ, LABSTACK_ROOT=str(tmp_path)),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--node" in result.stderr
    assert not (tmp_path / "logs").exists()
