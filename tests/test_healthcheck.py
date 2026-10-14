"""Run health commands through a local SSH boundary and check bounded failures."""

import subprocess
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from labstack import healthcheck
from labstack.inventory import nodes, read_config
from labstack.models.config import Config
from labstack.models.provisioning import Healthcheck
from labstack.models.state import State
from labstack.settings import get_settings


class LocalHealthCommandFake:
    """Run the real guest command in Bash instead of opening an SSH connection."""

    def __call__(self, node, command):
        return ["bash", "-c", command]


@pytest.fixture
def health_state(tmp_path, monkeypatch):
    """Use real command execution with test-owned logs and a live monitor stub."""
    definition = read_config().model_dump()
    for machine in definition["machines"].values():
        machine["depends_on"] = {}
    definition["machines"]["cp"]["healthcheck"] = {"test": ["CMD", "true"]}
    config = Config.model_validate(definition)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base" for node in nodes(config)},
    )
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)
    monkeypatch.setattr(healthcheck, "ssh_command", LocalHealthCommandFake())
    monkeypatch.setattr(healthcheck, "running", Mock(return_value=True))
    return state


def test_cmd_healthcheck_preserves_literal_arguments(health_state, tmp_path):
    definition = nodes(health_state.config)[0].model_dump()
    marker = tmp_path / "unexpected"
    literal = f"$(touch {marker})"
    definition["healthcheck"] = {"test": ["CMD", "printf", "%s", literal]}
    node = type(nodes(health_state.config)[0]).model_validate(definition)

    healthcheck.wait_for_health(node, health_state)

    log = (tmp_path / "cp-healthcheck.log").read_text()
    assert literal in log
    assert not marker.exists()


def test_shell_healthcheck_can_retry_until_a_service_is_ready(health_state, tmp_path):
    flag = tmp_path / "attempted"
    definition = nodes(health_state.config)[0].model_dump()
    definition["healthcheck"] = {
        "test": f"test -f '{flag}' || {{ touch '{flag}'; exit 1; }}",
        "interval": "0.01s",
        "retries": 2,
    }
    node = type(nodes(health_state.config)[0]).model_validate(definition)

    healthcheck.wait_for_health(node, health_state)

    assert flag.exists()
    assert "Attempt 2" in (tmp_path / "cp-healthcheck.log").read_text()


def test_exhausted_healthcheck_retains_output_and_stops(health_state, tmp_path):
    definition = nodes(health_state.config)[0].model_dump()
    definition["healthcheck"] = {
        "test": ["CMD-SHELL", "echo 'Service is unavailable' >&2; exit 4"],
        "interval": "0.01s",
        "retries": 2,
    }
    node = type(nodes(health_state.config)[0]).model_validate(definition)

    with pytest.raises(ValueError, match="cp.*healthcheck.*log"):
        healthcheck.wait_for_health(node, health_state)

    assert "Service is unavailable" in (tmp_path / "cp-healthcheck.log").read_text()


def test_hanging_commands_have_a_per_attempt_timeout(
    health_state, tmp_path, monkeypatch
):
    definition = nodes(health_state.config)[0].model_dump()
    definition["healthcheck"] = {
        "test": ["CMD", "sleep", "100"],
        "timeout": "0.1s",
        "retries": 1,
    }
    node = type(nodes(health_state.config)[0]).model_validate(definition)
    runner = Mock(
        side_effect=subprocess.TimeoutExpired("ssh", 0.1, output=b"partial output")
    )
    monkeypatch.setattr(healthcheck.subprocess, "run", runner)

    with pytest.raises(ValueError, match="healthcheck"):
        healthcheck.wait_for_health(node, health_state)

    assert runner.call_args.kwargs["timeout"] == 0.1
    log = (tmp_path / "cp-healthcheck.log").read_text()
    assert "timed out" in log
    assert "partial output" in log


def test_a_stopped_guest_fails_without_waiting_for_more_attempts(
    health_state, monkeypatch
):
    monkeypatch.setattr(healthcheck, "running", Mock(return_value=False))

    with pytest.raises(ValueError, match="stopped"):
        healthcheck.wait_for_health(nodes(health_state.config)[0], health_state)


@pytest.mark.parametrize(
    "test",
    [[], ["CMD"], ["CMD-SHELL", "true", "false"], ["NONE"], "", ["CMD", "a\x00b"]],
)
def test_invalid_health_commands_are_rejected(test):
    with pytest.raises(ValidationError, match="test"):
        Healthcheck(test=test)


@pytest.mark.parametrize(
    "field,value",
    [
        ("interval", "0s"),
        ("timeout", "nope"),
        ("retries", 0),
        ("timeout", "2h"),
        ("start_period", -1),
    ],
)
def test_invalid_or_unbounded_health_settings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Healthcheck.model_validate({"test": ["CMD", "true"], field: value})
