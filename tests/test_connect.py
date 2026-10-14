"""Check the public Task command without opening a real SSH session."""

import os
import subprocess

import pytest

from labstack.inventory import read_config
from labstack.models.config import Config
from labstack.models.state import State
from labstack.settings import get_settings


@pytest.fixture(params=["terminal", "github-actions"])
def saved_lab(tmp_path, request):
    """Save custom YAML names and record calls to the external SSH executable.

    Task, the command parser and saved-state validation remain real. The SSH spy
    records each call and prints its arguments to identify the selected guest.
    The call marker shows whether SSH ran even when Task writes CI annotations.
    All command-created paths stay inside the temporary lab.
    """
    definition = read_config().model_dump()
    renamed = dict(
        zip(definition["machines"], ["main", "worker-1", "extra"], strict=True)
    )
    definition["machines"] = {
        renamed[name]: machine for name, machine in definition["machines"].items()
    }
    for machine in definition["machines"].values():
        machine["depends_on"] = {
            renamed[name]: dependency
            for name, dependency in machine["depends_on"].items()
        }
    config = Config.model_validate(definition)
    directory = tmp_path / ".state"
    directory.mkdir()
    state = State(
        config=config,
        instance_id="connection-test",
        base_images={
            "main": tmp_path / "base.qcow2",
            "worker-1": tmp_path / "base.qcow2",
        },
    )
    (directory / "state.json").write_text(state.model_dump_json())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    ssh_spy = binaries / "ssh"
    ssh_spy.write_text(
        '#!/bin/sh\ntouch "$LABSTACK_ROOT/ssh-called"\nprintf "%s\\n" "$@"\n'
    )
    ssh_spy.chmod(0o755)
    environment = dict(
        os.environ, LABSTACK_ROOT=str(tmp_path), PATH=f"{binaries}:{os.environ['PATH']}"
    )
    # Test both Task output modes, regardless of where pytest itself runs.
    environment.pop("GITHUB_ACTIONS", None)
    if request.param == "github-actions":
        environment["GITHUB_ACTIONS"] = "true"
    return environment


@pytest.mark.parametrize("name", ["main", "worker-1"])
def test_task_connect_opens_the_machine_with_its_saved_yaml_name(
    tmp_path, saved_lab, name
):
    result = subprocess.run(
        [
            "task",
            "--taskfile",
            str(get_settings().root / "Taskfile.yml"),
            "connect",
            "--",
            name,
        ],
        env=saved_lab,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ssh-called").exists()
    assert result.stdout.splitlines() == [
        "-F",
        str(tmp_path / ".state/ssh_config"),
        name,
    ]


@pytest.mark.parametrize("name", ["missing", "extra"])
def test_task_connect_rejects_unknown_or_inactive_machines(tmp_path, saved_lab, name):
    result = subprocess.run(
        [
            "task",
            "--taskfile",
            str(get_settings().root / "Taskfile.yml"),
            "connect",
            "--",
            name,
        ],
        env=saved_lab,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Available machines: main, worker-1" in result.stderr
    assert not (tmp_path / "ssh-called").exists()


def test_task_connect_requires_a_machine_name(tmp_path, saved_lab):
    result = subprocess.run(
        ["task", "--taskfile", str(get_settings().root / "Taskfile.yml"), "connect"],
        env=saved_lab,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "task connect -- cp" in result.stderr
    assert not (tmp_path / "ssh-called").exists()
    assert not (tmp_path / "logs").exists()


def test_task_connect_treats_shell_text_as_a_machine_name(tmp_path, saved_lab):
    marker = tmp_path / "unexpected"
    result = subprocess.run(
        [
            "task",
            "--taskfile",
            str(get_settings().root / "Taskfile.yml"),
            "connect",
            "--",
            f"$(touch {marker})",
        ],
        env=saved_lab,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Available machines: main, worker-1" in result.stderr
    assert not (tmp_path / "ssh-called").exists()
    assert not marker.exists()
