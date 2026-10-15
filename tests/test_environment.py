"""Keep machine variables literal, private and stable for the life of a lab."""

import json
import shlex
import subprocess

import pytest
import yaml
from pydantic import ValidationError

from labstack.cloud_init import cloud_config
from labstack.cluster import read_state, save_state
from labstack.environment import resolve_environments, shell_environment
from labstack.inventory import nodes, read_config, resolve_config
from labstack.models.config import Config
from labstack.models.state import State
from labstack.settings import get_settings


@pytest.fixture
def environment_inventory(tmp_path):
    """Use a complete inventory with files beside it, independent of the working directory."""
    definition = read_config().model_dump(mode="json")
    definition["machines"]["cp"]["env_file"] = ["common.env", "machine.env"]
    definition["machines"]["cp"]["environment"] = {"LOG_LEVEL": "debug"}
    (tmp_path / "common.env").write_text(
        "LAB_NAME=cka\nLOG_LEVEL=warning\nORDER=first\n"
    )
    (tmp_path / "machine.env").write_text("LOG_LEVEL=info\nORDER=last\n")
    path = tmp_path / "machines.yml"
    path.write_text(yaml.safe_dump(definition, sort_keys=False))
    return path


def test_environment_files_are_ordered_and_yaml_values_win(environment_inventory):
    config = read_config(environment_inventory)
    values = resolve_environments(config, environment_inventory.parent)

    assert values["cp"] == {"LAB_NAME": "cka", "LOG_LEVEL": "debug", "ORDER": "last"}
    assert values["w1"] == {}
    assert "w2" not in values


def test_single_env_file_is_resolved_relative_to_inventory(environment_inventory):
    definition = yaml.safe_load(environment_inventory.read_text())
    definition["machines"]["cp"]["env_file"] = "common.env"
    environment_inventory.write_text(yaml.safe_dump(definition))
    config = read_config(environment_inventory)

    assert config.machines["cp"].env_file == ("common.env",)
    assert (
        resolve_environments(config, environment_inventory.parent)["cp"]["LAB_NAME"]
        == "cka"
    )


def test_empty_yaml_value_overrides_a_nonempty_file_value(environment_inventory):
    definition = yaml.safe_load(environment_inventory.read_text())
    definition["machines"]["cp"]["environment"]["LAB_NAME"] = ""
    config = Config.model_validate(definition)

    values = resolve_environments(config, environment_inventory.parent)

    assert values["cp"]["LAB_NAME"] == ""


def test_inactive_profiles_do_not_require_their_environment_files(
    environment_inventory,
):
    definition = yaml.safe_load(environment_inventory.read_text())
    definition["machines"]["w2"]["env_file"] = ["optional.env"]
    config = Config.model_validate(definition)

    assert "w2" not in resolve_environments(config, environment_inventory.parent)
    definition["profiles"] = ["extra-worker"]
    with pytest.raises(ValueError, match="optional.env"):
        resolve_environments(
            Config.model_validate(definition), environment_inventory.parent
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", {"BAD-NAME": "value"}),
        ("environment", {"BAD=NAME": "value"}),
        ("environment", {"1NAME": "value"}),
        ("environment", {"LAB_NAME": None}),
        ("environment", {"LAB_NAME": True}),
        ("environment", {"LAB_NAME": 42}),
        ("environment", {"LAB_NAME": "a\x00b"}),
        ("environment", {"LAB_NAME": "a\nb"}),
        ("environment", {"LAB_NAME": "a\rb"}),
        ("environment", {"NODE_NAME": "other"}),
        ("environment", {"SSH_USER": "root"}),
        ("env_file", ["../outside.env"]),
        ("env_file", ["/tmp/outside.env"]),
        ("env_file", [""]),
    ],
)
def test_invalid_machine_environment_is_rejected_without_values(field, value):
    definition = read_config().model_dump()
    definition["machines"]["cp"][field] = value

    with pytest.raises(ValidationError, match=field) as caught:
        Config.model_validate(definition)

    assert "input_value" not in str(caught.value)


@pytest.mark.parametrize(
    "contents",
    [
        "BAD-NAME=private-value\n",
        "MISSING_VALUE\n",
        'BROKEN="private-value\n',
        "SSH_USER=root\n",
        'MULTILINE="a\\nb"\n',
    ],
)
def test_invalid_environment_file_errors_do_not_expose_values(
    environment_inventory, contents
):
    environment_inventory.with_name("common.env").write_text(contents)

    with pytest.raises(ValueError, match=r"common.env.*line 1") as caught:
        resolve_environments(
            read_config(environment_inventory), environment_inventory.parent
        )

    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("symlink", [False, True])
def test_missing_or_external_env_files_fail_before_guest_creation(
    environment_inventory, tmp_path, symlink
):
    path = tmp_path / "common.env"
    path.unlink()
    if symlink:
        outside = tmp_path.parent / f"{tmp_path.name}-outside.env"
        outside.write_text("LAB_NAME=outside\n")
        path.symlink_to(outside)

    with pytest.raises(ValueError, match="environment file"):
        resolve_environments(read_config(environment_inventory), tmp_path)


@pytest.mark.parametrize(
    "contents",
    [b"X=" + b"a" * 1024**2, b"X=\xff\n"],
    # Pytest otherwise includes the full 1 MiB payload in logs and JUnit case names.
    ids=["oversized", "invalid-utf8"],
)
def test_oversized_or_non_utf8_environment_files_are_rejected(
    environment_inventory, contents
):
    environment_inventory.with_name("common.env").write_bytes(contents)

    with pytest.raises(ValueError, match=r"[Ee]nvironment file.*common.env"):
        resolve_environments(
            read_config(environment_inventory), environment_inventory.parent
        )


def test_dotenv_quotes_comments_and_dollars_stay_literal(
    environment_inventory, monkeypatch
):
    monkeypatch.setenv("HOST_VALUE", "must-not-be-used")
    environment_inventory.with_name("common.env").write_text(
        '# A normal dotenv file\nexport LAB_NAME="cka lab"\n'
        "SINGLE='quoted # text'\nEMPTY=\n"
        "LITERAL=${HOST_VALUE}\nLOG_LEVEL=info # comment\n"
    )

    values = resolve_environments(
        read_config(environment_inventory), environment_inventory.parent
    )

    assert values["cp"] == {
        "LAB_NAME": "cka lab",
        "SINGLE": "quoted # text",
        "EMPTY": "",
        "LITERAL": "${HOST_VALUE}",
        "LOG_LEVEL": "debug",
        "ORDER": "last",
    }


def test_cloud_init_prepares_private_shell_and_systemd_files(
    environment_inventory, tmp_path
):
    config = read_config(environment_inventory)
    state = State(
        config=config,
        instance_id="environment-test",
        base_images=dict.fromkeys(config.active_machines, tmp_path / "base"),
        environments=resolve_environments(config, tmp_path),
    )

    data = cloud_config(nodes(config)[0], state, "ssh-ed25519 TEST")
    files = {item["path"]: item for item in data["write_files"]}

    for path in ("/etc/labstack/environment", "/etc/labstack/environment.sh"):
        assert files[path]["permissions"] == "0640"
        assert files[path]["owner"] == "root:ubuntu"
        assert files[path]["defer"] is True
        assert "LAB_NAME" in files[path]["content"]
    assert (
        "/etc/labstack/environment.sh" in files["/etc/profile.d/labstack.sh"]["content"]
    )
    assert files["/etc/profile.d/labstack.sh"]["owner"] == "root:root"


def test_generated_shell_preserves_values_without_execution(tmp_path):
    marker = tmp_path / "unexpected"
    values = {
        "LAB_NAME": "CKA lab",
        "EMPTY": "",
        "QUOTE": "single ' and double \"",
        "PAYLOAD": f"$(touch {marker}); `touch {marker}`; $HOME",
        "BACKSLASH": r"C:\some\path",
        "UNICODE": "été",
        "TAB": "a\tb",
    }
    script = shell_environment(values) + shlex.join(
        [
            "python3",
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ]
    )

    result = subprocess.run(
        ["bash", "-s"], input=script, text=True, capture_output=True, check=True
    )

    actual = json.loads(result.stdout)
    assert {key: actual[key] for key in values} == values
    assert not marker.exists()


def test_saved_environments_survive_changed_and_removed_files(
    environment_inventory, tmp_path, monkeypatch
):
    config = read_config(environment_inventory)
    state = State(
        config=config,
        instance_id="environment-test",
        base_images=dict.fromkeys(config.active_machines, tmp_path / "base"),
        environments=resolve_environments(config, tmp_path),
    )
    state_dir = tmp_path / ".state"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    monkeypatch.setattr(get_settings(), "state_dir", state_dir)
    save_state(state)
    (tmp_path / "common.env").write_text("LAB_NAME=changed\n")
    (tmp_path / "machine.env").unlink()

    saved = read_state()

    assert resolve_config(saved.config, None) == config
    assert saved.environment_for("cp")["LAB_NAME"] == "cka"
    assert saved.environment_for("cp")["LOG_LEVEL"] == "debug"
    assert (state_dir / "state.json").stat().st_mode & 0o777 == 0o600


def test_saved_environments_must_cover_exactly_the_active_machines(tmp_path):
    config = read_config()

    with pytest.raises(ValidationError, match="Saved environments"):
        State(
            config=config,
            instance_id="bad-snapshot",
            base_images=dict.fromkeys(config.active_machines, tmp_path / "base"),
            environments={"cp": {"LAB_NAME": "value"}},
        )
