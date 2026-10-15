import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from labstack import inventory
from labstack.models.config import Config
from labstack.models.state import State
from labstack.settings import get_settings
from tests.runtime import POLICY_CHECK, save_diagnostic, ssh_key_identity


@pytest.mark.parametrize("account", ["debian", "vagrant"])
def test_guest_runtime_checks_receive_the_saved_account(tmp_path, monkeypatch, account):
    from tests import runtime

    definition = inventory.read_config().model_dump()
    definition["machines"]["cp"]["ssh_user"] = account
    state = State(
        config=Config.model_validate(definition),
        instance_id="runtime-test",
        base_images=dict.fromkeys(("cp", "w1"), tmp_path / "base"),
    )
    # Stop at identity validation so the test never reaches networking or Kubernetes.
    remote_stub = Mock(side_effect=["guest checks", "invalid", "ssh-ed25519 KEY cp"])
    monkeypatch.setattr(runtime, "remote", remote_stub)
    monkeypatch.setattr(runtime, "save_diagnostic", Mock(return_value=True))
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)

    with pytest.raises(ValueError, match="Invalid machine identity"):
        runtime.check_environment(state)

    node, command, script = remote_stub.call_args_list[0].args
    assert node.ssh_user == account
    assert command == f"bash -s -- 20 {account}"
    assert script == runtime.GUEST_CHECK


def test_host_key_comparison_ignores_the_hostname_comment():
    first = "ssh-ed25519 SAME-KEY ubuntu@control-plane"
    second = "ssh-ed25519 SAME-KEY ubuntu@worker-1"

    assert ssh_key_identity(first) == ssh_key_identity(second)


def test_missing_ssh_configuration_keeps_an_error_log(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "state_dir", tmp_path / "missing-state")
    output = tmp_path / "diagnostics.log"

    success = save_diagnostic(
        inventory.nodes(inventory.read_config())[0], "true", output
    )

    assert success is False
    assert "No such file" in output.read_text()


@pytest.mark.parametrize(
    ("behavior", "error"),
    [
        ("transport-error", "API exec transport failed"),
        ("policy-not-enforced", "Expected DENIED from client, got ALLOWED"),
    ],
)
def test_policy_checks_fail_on_exec_errors_or_ignored_policy(tmp_path, behavior, error):
    kubectl_fake = tmp_path / "kubectl"
    kubectl_fake.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "import yaml\n"
        "policy_state = Path(os.environ['POLICY_STATE'])\n"
        "if 'exec' in sys.argv:\n"
        "    if policy_state.exists() and os.environ['PROBE_BEHAVIOR'] == 'transport-error':\n"
        "        print('API exec transport failed', file=sys.stderr)\n"
        "        sys.exit(42)\n"
        "    print('ALLOWED')\n"
        "elif 'get' in sys.argv:\n"
        "    print('10.96.0.30')\n"
        "elif 'apply' in sys.argv:\n"
        "    resource = yaml.safe_load(sys.stdin)\n"
        "    if resource['kind'] == 'NetworkPolicy':\n"
        "        policy_state.write_text(resource['metadata']['name'])\n"
    )
    kubectl_fake.chmod(0o700)
    policy_state = tmp_path / "applied-policy"
    sleep_stub = tmp_path / "sleep"
    sleep_stub.write_text("#!/bin/sh\nexit 0\n")
    sleep_stub.chmod(0o700)

    result = subprocess.run(
        ["bash", "-s", "--", "smoke-test", "w1"],
        input=POLICY_CHECK,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PROBE_BEHAVIOR": behavior,
            "POLICY_STATE": str(policy_state),
        },
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert error in result.stderr
    # The baseline must succeed first. An exec failure during the denial check is
    # the case that could otherwise be mistaken for working policy enforcement.
    assert policy_state.read_text() == "deny-web"
    assert "NetworkPolicy checks passed" not in result.stdout


def test_workload_checks_use_named_nodes_and_probe_each_extra_worker(tmp_path):
    """Keep runtime pod placement correct when profiles add several guests."""
    import json

    from tests.runtime import WORKLOAD_CHECK

    calls = tmp_path / "kubectl-calls.jsonl"
    executable = tmp_path / "kubectl"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "arguments = sys.argv[1:]\n"
        "body = sys.stdin.read() if 'apply' in arguments else ''\n"
        "with open(os.environ['KUBECTL_CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps({'arguments': arguments, 'body': body}) + '\\n')\n"
    )
    executable.chmod(0o700)

    result = subprocess.run(
        [
            "bash",
            "-s",
            "--",
            "smoke-test",
            "main",
            "compute-a",
            "compute-b",
            "compute-c",
        ],
        input=WORKLOAD_CHECK,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "KUBECTL_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    manifest = recorded[1]["body"]
    assert 'hostname: "main"' in manifest
    assert 'hostname: "compute-a"' in manifest
    peers = [entry["arguments"][3] for entry in recorded if "run" in entry["arguments"]]
    assert peers == ["peer-compute-b", "peer-compute-c"]
