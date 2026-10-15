"""Keep each machine's login, sudo rights and files on its selected account."""

import subprocess

import pytest
import yaml
from pydantic import ValidationError

from labstack.cloud_init import cloud_config
from labstack.inventory import nodes, read_config
from labstack.models.config import Config
from labstack.models.state import State
from labstack.settings import get_settings
from labstack.ssh import write_ssh_config


@pytest.fixture
def access_state(tmp_path):
    """Read two different guest accounts through the public YAML inventory."""
    definition = read_config().model_dump(mode="json")
    definition["machines"]["cp"]["ssh_user"] = "debian"
    definition["machines"]["w1"]["ssh_user"] = "vagrant"
    inventory = tmp_path / "machines.yml"
    inventory.write_text(yaml.safe_dump(definition))
    return State(
        config=read_config(inventory),
        instance_id="access-test",
        base_images=dict.fromkeys(("cp", "w1"), tmp_path / "base.qcow2"),
    )


def test_yaml_accounts_reach_cloud_init_sudo_and_authorized_keys(access_state):
    key = "ssh-ed25519 TEST lab"
    for node, account in zip(
        nodes(access_state.config), ("debian", "vagrant"), strict=True
    ):
        data = cloud_config(node, access_state, key)

        assert data["users"][0]["name"] == account
        assert data["users"][0]["homedir"] == f"/home/{account}"
        assert data["users"][0]["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
        assert "sudo" in data["users"][0]["groups"]
        assert data["users"][0]["lock_passwd"] is True
        assert data["ssh_pwauth"] is False
        assert data["disable_root"] is True
        authorized_keys = next(
            entry
            for entry in data["write_files"]
            if entry["path"] == f"/home/{account}/.ssh/authorized_keys"
        )
        assert authorized_keys["owner"] == f"{account}:{account}"
        assert authorized_keys["permissions"] == "0600"
        assert authorized_keys["defer"] is True
        assert authorized_keys["content"] == key + "\n"


def test_openssh_selects_each_saved_account(access_state, tmp_path, monkeypatch):
    directory = tmp_path / ".state"
    directory.mkdir()
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    write_ssh_config(access_state)

    for name, account in (("cp", "debian"), ("w1", "vagrant")):
        # Let OpenSSH parse the file: a wildcard User can hide a later machine value.
        result = subprocess.run(
            ["ssh", "-G", "-F", str(directory / "ssh_config"), name],
            capture_output=True,
            text=True,
            check=True,
        )
        assert f"user {account}" in result.stdout.splitlines()


@pytest.mark.parametrize("account", ["debian", "vagrant", "cloud-user", "_lab"])
def test_machine_accepts_safe_linux_accounts(account):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["ssh_user"] = account

    config = Config.model_validate(definition)

    assert nodes(config)[0].ssh_user == account


@pytest.mark.parametrize(
    "account",
    [None, "", "root", "../root", "a b", "$(id)", "user\nUser root", "a" * 33],
)
def test_machine_rejects_unsafe_or_root_accounts(account):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["ssh_user"] = account

    with pytest.raises(ValidationError, match="ssh_user"):
        Config.model_validate(definition)


def test_saved_inventory_without_an_account_keeps_ubuntu(tmp_path, published_box):
    definition = read_config().model_dump()
    for machine in definition["machines"].values():
        machine.pop("ssh_user", None)
    state = State.model_validate(
        {
            "config": definition,
            "instance_id": "existing-lab",
            "base_images": {"cp": tmp_path / "base", "w1": tmp_path / "base"},
            "resolved_boxes": dict.fromkeys(
                ("cp", "w1"),
                published_box.model_dump(mode="json", exclude={"ssh_user"}),
            ),
        }
    )

    assert all(node.ssh_user == "ubuntu" for node in nodes(state.config))
    assert all(pin.ssh_user == "ubuntu" for pin in state.resolved_boxes.values())
