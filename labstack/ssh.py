"""Keep guest access and failure diagnostics inside the project environment."""

import subprocess
import time

from labstack.inventory import nodes
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.qemu import running
from labstack.settings import get_settings


def ssh_command(node: Node, command: str | None = None) -> list[str]:
    """Build SSH arguments using only this environment's access configuration.

    Script calls use BatchMode so they cannot wait for an interactive password prompt.
    An omitted command opens a normal shell. Credentials and accepted guest host keys
    are kept separate from the user's global SSH configuration.
    """
    settings = get_settings()
    arguments = ["ssh", "-F", str(settings.state_dir / "ssh_config")]
    if command is not None:
        arguments += ["-o", "BatchMode=yes", node.name, command]
    else:
        arguments += [node.name]
    return arguments


def remote(node: Node, command: str, script: str | None = None) -> str:
    """Run a guest command and preserve its output if it fails.

    Successful output is returned to the caller for status or configuration parsing.
    Failed output goes to the node's diagnostic log; the raised error names that log
    rather than copying potentially sensitive provisioning output into application logs.
    """
    settings = get_settings()
    result = subprocess.run(
        ssh_command(node, command),
        input=script,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode:
        with (settings.logs_dir / f"{node.name}-provision.log").open("a") as stream:
            stream.write(result.stdout + result.stderr)
        raise ValueError(
            f"Command failed on {node.name}. See logs/{node.name}-provision.log."
        )
    return result.stdout


def write_ssh_config(state: State) -> None:
    """Create guest SSH entries with project keys and isolated known hosts.

    Accepting a new host key allows first boot without an interactive prompt. Later
    connections still check that key, and a fresh environment gets a fresh known-hosts
    file. Paths are quoted so a project directory containing spaces remains usable.
    Each Host entry sets its own login; a wildcard User would hide later overrides.
    """
    settings = get_settings()
    text = (
        f'Host *\n  HostName 127.0.0.1\n  IdentityFile "{settings.state_dir / "id_ed25519"}"\n'
        f'  UserKnownHostsFile "{settings.state_dir / "known_hosts"}"\n  StrictHostKeyChecking accept-new\n'
        "  IdentitiesOnly yes\n  ConnectTimeout 5\n  ServerAliveInterval 15\n"
    )
    for node in nodes(state.config):
        text += f"Host {node.name}\n  Port {node.ssh_port}\n  User {node.ssh_user}\n"
    (settings.state_dir / "ssh_config").write_text(text)


def wait_for_guest(node: Node, state: State) -> None:
    """Wait for SSH, then require cloud-init to finish before running scripts.

    The monitor is checked while waiting so a stopped guest fails promptly rather than
    waiting for the full SSH timeout. An SSH connection alone does not mean network,
    access and disk setup have completed; cloud-init failures stop provisioning.
    """
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if not running(node, state):
            raise ValueError(f"{node.name} stopped during boot. See its serial log.")
        result = subprocess.run(ssh_command(node, "true"), capture_output=True)
        if result.returncode == 0:
            # SSH can be available before cloud-init finishes network and disk setup.
            remote(node, "sudo cloud-init status --wait --long")
            return
        time.sleep(2)
    raise ValueError(f"SSH timed out on {node.name}. See logs/{node.name}-serial.log.")
