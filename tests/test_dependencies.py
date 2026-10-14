"""Check readiness checkpoints and dependency ordering without virtual machines."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from labstack import provisioning
from labstack.inventory import nodes, read_config
from labstack.models.config import Config
from labstack.models.state import State
from labstack.settings import get_settings


class ScriptRunnerSpy:
    """Record successful guest scripts without starting external processes."""

    def __init__(self):
        self.steps = []

    def __call__(self, node, path, state):
        self.steps.append((node.name, path))


@pytest.fixture
def dependency_inventory(tmp_path, monkeypatch):
    """Give each guest real scripts and remove the default Kubernetes recipe."""
    definition = read_config().model_dump()
    directory = tmp_path / "provision" / "example"
    directory.mkdir(parents=True)
    for name in ("base", "server", "client", "addons"):
        (directory / f"{name}.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    for machine in definition["machines"].values():
        machine.pop("healthcheck", None)
        machine.pop("depends_on", None)
        machine["scripts"] = ["provision/example/base.sh"]
    definition["machines"]["cp"]["scripts"] += [
        "provision/example/server.sh",
        "provision/example/addons.sh",
    ]
    definition["machines"]["w1"]["scripts"] += ["provision/example/client.sh"]
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)
    monkeypatch.setattr(provisioning, "sync_shared", Mock())
    return definition


def test_healthcheck_can_release_workers_before_control_plane_addons(
    dependency_inventory, monkeypatch
):
    cp = dependency_inventory["machines"]["cp"]
    cp["healthcheck"] = {
        "test": ["CMD", "true"],
        "after": "provision/example/server.sh",
    }
    cp["depends_on"] = {
        "w1": {"condition": "completed", "before": "provision/example/addons.sh"}
    }
    dependency_inventory["machines"]["w1"]["depends_on"] = {
        "cp": {"condition": "healthy", "before": "provision/example/client.sh"}
    }
    config = Config.model_validate(dependency_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    runner = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner)
    health = Mock()
    monkeypatch.setattr(provisioning, "wait_for_health", health)

    provisioning.provision(state)

    assert (
        runner.steps.index(("cp", "provision/example/server.sh"))
        < runner.steps.index(("w1", "provision/example/client.sh"))
        < runner.steps.index(("cp", "provision/example/addons.sh"))
    )
    health.assert_called_once()
    assert health.call_args.args[0].name == "cp"


def test_failed_healthcheck_prevents_dependent_scripts(
    dependency_inventory, monkeypatch
):
    dependency_inventory["machines"]["cp"]["healthcheck"] = {"test": ["CMD", "false"]}
    dependency_inventory["machines"]["w1"]["depends_on"] = {
        "cp": {"condition": "healthy"}
    }
    config = Config.model_validate(dependency_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    runner = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner)
    monkeypatch.setattr(
        provisioning, "wait_for_health", Mock(side_effect=ValueError("unhealthy"))
    )

    with pytest.raises(ValueError, match="unhealthy"):
        provisioning.provision(state)

    assert all(name != "w1" for name, _ in runner.steps)


def test_completed_dependency_works_when_lists_have_different_lengths(
    dependency_inventory, monkeypatch
):
    dependency_inventory["machines"]["w1"]["depends_on"] = {
        "cp": {"condition": "completed"}
    }
    config = Config.model_validate(dependency_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    runner = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner)

    provisioning.provision(state)

    assert runner.steps == [
        ("cp", "provision/example/base.sh"),
        ("cp", "provision/example/server.sh"),
        ("cp", "provision/example/addons.sh"),
        ("w1", "provision/example/base.sh"),
        ("w1", "provision/example/client.sh"),
    ]


@pytest.mark.parametrize("condition", ["started", "completed", "healthy"])
def test_required_dependencies_cannot_be_inactive(dependency_inventory, condition):
    dependency_inventory["machines"]["w2"]["healthcheck"] = {"test": ["CMD", "true"]}
    dependency_inventory["machines"]["cp"]["depends_on"] = {
        "w2": {"condition": condition}
    }

    with pytest.raises(ValidationError, match="inactive"):
        Config.model_validate(dependency_inventory)


def test_optional_dependencies_skip_a_disabled_profile(dependency_inventory):
    dependency_inventory["machines"]["cp"]["depends_on"] = {
        "w2": {"condition": "completed", "required": False}
    }

    assert [
        node.name for node in nodes(Config.model_validate(dependency_inventory))
    ] == ["cp", "w1"]


@pytest.mark.parametrize("condition", ["healthy", "completed"])
def test_dependency_cycles_fail_before_any_vm_is_created(
    dependency_inventory, condition
):
    for name, dependency in (("cp", "w1"), ("w1", "cp")):
        dependency_inventory["machines"][name]["healthcheck"] = {
            "test": ["CMD", "true"]
        }
        dependency_inventory["machines"][name]["depends_on"] = {
            dependency: {"condition": condition}
        }

    with pytest.raises(ValidationError, match="cycle"):
        Config.model_validate(dependency_inventory)


@pytest.mark.parametrize(
    "dependency,message",
    [
        ({"missing": {"required": False}}, "Unknown dependency"),
        ({"cp": {"condition": "healthy"}}, "healthcheck"),
        ({"cp": {"before": "provision/example/missing.sh"}}, "before"),
        ({"w1": {}}, "itself"),
    ],
)
def test_invalid_dependency_references_are_rejected(
    dependency_inventory, dependency, message
):
    dependency_inventory["machines"]["w1"]["depends_on"] = dependency

    with pytest.raises(ValidationError, match=message):
        Config.model_validate(dependency_inventory)


def test_healthcheck_checkpoint_must_name_an_existing_script(dependency_inventory):
    dependency_inventory["machines"]["cp"]["healthcheck"] = {
        "test": ["CMD", "true"],
        "after": "provision/example/missing.sh",
    }

    with pytest.raises(ValidationError, match="after"):
        Config.model_validate(dependency_inventory)


def test_a_guest_without_scripts_waits_before_its_healthcheck(
    dependency_inventory, monkeypatch
):
    dependency_inventory["machines"]["w1"]["scripts"] = []
    dependency_inventory["machines"]["w1"]["healthcheck"] = {"test": ["CMD", "true"]}
    dependency_inventory["machines"]["w1"]["depends_on"] = {
        "cp": {"condition": "completed"}
    }
    config = Config.model_validate(dependency_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    health = Mock(side_effect=ValueError("health checkpoint"))
    runner = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "wait_for_health", health)
    monkeypatch.setattr(provisioning, "run_script", runner)

    with pytest.raises(ValueError, match="health checkpoint"):
        provisioning.provision(state)

    assert runner.steps[-1] == ("cp", "provision/example/addons.sh")
