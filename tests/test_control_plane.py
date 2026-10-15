"""Keep the guest's admin kubeconfig on its configured account."""

import json
import os
import subprocess
import sys

import pytest

from labstack.settings import get_settings


@pytest.mark.parametrize("account", ["ubuntu", "debian", "vagrant"])
def test_control_plane_uses_the_account_home_and_primary_group(tmp_path, account):
    """Run the real shell recipe while recording every system installation call."""
    directory = tmp_path / "bin"
    directory.mkdir()
    installs = tmp_path / "install-calls.jsonl"
    guest_home = tmp_path / "guest-home"
    (directory / "install").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['INSTALL_CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    (directory / "getent").write_text(
        '#!/bin/sh\nprintf "%s:x:1000:1000:Lab:%s:/bin/bash\\n" "$2" "$GUEST_HOME"\n'
    )
    (directory / "id").write_text('#!/bin/sh\nprintf "%s\\n" lab-users\n')
    (directory / "kubeadm").write_text(
        '#!/bin/sh\nif [ "$1" = token ]; then printf "%s\\n" "kubeadm join test"; fi\n'
    )
    for executable in directory.iterdir():
        executable.chmod(0o700)
    shared = tmp_path / "shared"
    shared.mkdir()

    result = subprocess.run(
        ["bash", str(get_settings().root / "provision/kubernetes/control-plane.sh")],
        env={
            **os.environ,
            "PATH": f"{directory}:{os.environ['PATH']}",
            "SSH_USER": account,
            "GUEST_HOME": str(guest_home),
            "INSTALL_CALLS": str(installs),
            "NODE_IP": "192.168.76.10",
            "KUBERNETES_VERSION": "1.37.1",
            "SHARED_DIR": str(shared),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in installs.read_text().splitlines()]
    assert calls[:2] == [
        [
            "-d",
            "-m",
            "0700",
            "-o",
            account,
            "-g",
            "lab-users",
            str(guest_home / ".kube"),
        ],
        [
            "-m",
            "0600",
            "-o",
            account,
            "-g",
            "lab-users",
            "/etc/kubernetes/admin.conf",
            str(guest_home / ".kube/config"),
        ],
    ]
    assert (shared / "join-command").read_text() == "kubeadm join test\n"
