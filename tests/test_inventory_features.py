"""Check named guests, reusable YAML blocks and profile selection offline."""

import pytest
import yaml
from pydantic import ValidationError

from labstack.inventory import nodes, read_config, resolve_config
from labstack.models.config import Config
from labstack.settings import get_settings


@pytest.fixture
def named_inventory():
    """Describe the same guests using the public keyed inventory format."""
    definition = yaml.safe_load((get_settings().root / "machines.yml").read_text())
    definition["machines"]["w2"]["profiles"] = ["extra-worker"]
    return definition


def test_machine_keys_supply_hostnames_and_disk_names(named_inventory):
    config = Config.model_validate(named_inventory)

    assert [node.name for node in nodes(config)] == ["cp", "w1"]
    assert config.machines["cp"].cpus == 2
    assert "name" not in config.machines["cp"].model_dump()


def test_a_profile_starts_its_machine_without_changing_existing_macs(named_inventory):
    default = Config.model_validate(named_inventory)
    named_inventory["profiles"] = ["extra-worker"]
    expanded = Config.model_validate(named_inventory)

    assert [node.name for node in nodes(expanded)] == ["cp", "w1", "w2"]
    assert [node.cluster_mac for node in nodes(default)] == [
        node.cluster_mac for node in nodes(expanded)[:2]
    ]


def test_disabled_profiles_do_not_renumber_following_machines(named_inventory):
    machines = named_inventory["machines"]
    named_inventory["machines"] = {
        "cp": machines["cp"],
        "w2": machines["w2"],
        "w1": machines["w1"],
    }
    config = Config.model_validate(named_inventory)

    assert nodes(config)[1].index == 2
    assert nodes(config)[1].cluster_mac == "52:54:00:76:01:02"


def test_unknown_profiles_fail_before_downloading_images(named_inventory):
    named_inventory["profiles"] = ["typo"]

    with pytest.raises(ValidationError, match="Unknown profiles"):
        Config.model_validate(named_inventory)


def test_all_machines_cannot_be_disabled(named_inventory):
    for machine in named_inventory["machines"].values():
        machine["profiles"] = ["optional"]

    with pytest.raises(ValidationError, match="active machine"):
        Config.model_validate(named_inventory)


@pytest.mark.parametrize("name", ["../cp", "CP", "a b", "$(id)", ""])
def test_machine_keys_must_be_safe_hostnames(named_inventory, name):
    named_inventory["machines"][name] = named_inventory["machines"].pop("cp")

    with pytest.raises(ValidationError):
        Config.model_validate(named_inventory)


def test_extension_blocks_and_merge_anchors_are_resolved_before_validation(tmp_path):
    path = tmp_path / "machines.yml"
    path.write_text(
        """boxes_catalogue: https://cdelgehier.github.io/image-factory/
x-defaults: &defaults
  box: ubuntu-26.04-arm64
  cpus: 1
  memory_mib: 1536
  ports: ["52222:22"]
  networks:
    nat: {}
    lab: {ipv4_address: 192.168.76.10}
x-scripts: &scripts [provision/base/packages.sh]
machines:
  utility:
    <<: *defaults
    cpus: 2
    scripts: *scripts
networks:
  nat: {driver: nat}
  lab: {driver: socket, subnet: 192.168.76.0/24}
"""
    )

    config = read_config(path)

    assert nodes(config)[0].name == "utility"
    assert nodes(config)[0].cpus == 2
    assert config.machines["utility"].scripts == ("provision/base/packages.sh",)
    assert "x-defaults" not in config.model_dump()


def test_unknown_root_fields_still_fail(named_inventory):
    named_inventory["profiels"] = ["extra-worker"]

    with pytest.raises(ValidationError, match="Extra inputs"):
        Config.model_validate(named_inventory)


def test_duplicate_yaml_keys_cannot_replace_a_machine_silently(tmp_path):
    path = tmp_path / "machines.yml"
    path.write_text("machines:\n  cp: {}\n  cp: {}\n")

    with pytest.raises(ValueError, match="Duplicate YAML key.*cp"):
        read_config(path)


def test_profile_selection_is_saved_across_resumes(
    named_inventory, tmp_path, monkeypatch
):
    (tmp_path / "machines.yml").write_text(
        yaml.safe_dump(named_inventory, sort_keys=False)
    )
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    existing = resolve_config(None, ("extra-worker",))

    assert resolve_config(existing, None) == existing
    assert existing.profiles == ("extra-worker",)
    with pytest.raises(ValueError, match="task destroy"):
        resolve_config(existing, ())


def test_resume_rejects_reordering_that_would_change_mac_addresses(
    named_inventory, tmp_path, monkeypatch
):
    existing = Config.model_validate(named_inventory)
    machines = named_inventory["machines"]
    named_inventory["machines"] = {
        "cp": machines["cp"],
        "w2": machines["w2"],
        "w1": machines["w1"],
    }
    (tmp_path / "machines.yml").write_text(
        yaml.safe_dump(named_inventory, sort_keys=False)
    )
    monkeypatch.setattr(get_settings(), "root", tmp_path)

    with pytest.raises(ValueError, match="task destroy"):
        resolve_config(existing, None)


def test_first_active_guest_hosts_the_qemu_hub_even_if_its_index_is_not_zero(
    named_inventory, tmp_path
):
    from labstack.models.state import State
    from labstack.qemu import qemu_command

    # Skip an optional definition placed before the normal hub machine.
    machines = named_inventory["machines"]
    named_inventory["machines"] = {
        "w2": machines["w2"],
        "cp": machines["cp"],
        "w1": machines["w1"],
    }
    config = Config.model_validate(named_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base" for node in nodes(config)},
    )
    cp, worker = nodes(config)

    command = qemu_command(cp, state, tmp_path)

    assert cp.index == 1
    assert "hubport,id=cluster,hubid=0" in command
    assert f"hubport,id=port{worker.index},hubid=0,netdev=link{worker.index}" in command
    assert "server=off" in next(
        value
        for value in qemu_command(worker, state, tmp_path)
        if value.startswith("stream,")
    )


def test_nested_merge_anchors_keep_their_explicit_resource_overrides(tmp_path):
    path = tmp_path / "machines.yml"
    path.write_text(
        """boxes_catalogue: https://cdelgehier.github.io/image-factory/
x-defaults: &defaults
  box: ubuntu-26.04-arm64
  cpus: 1
  memory_mib: 1536
  ports: ["52222:22"]
  networks:
    nat: {}
    lab: {ipv4_address: 192.168.76.10}
x-recipes:
  resources:
    ubuntu: &custom
      <<: *defaults
      cpus: 2
machines:
  utility:
    <<: *custom
networks:
  nat: {driver: nat}
  lab: {driver: socket, subnet: 192.168.76.0/24}
"""
    )

    assert nodes(read_config(path))[0].cpus == 2
