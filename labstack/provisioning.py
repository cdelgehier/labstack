"""Run dependency-ordered inventory scripts and exchange their private shared files."""

import shlex
import subprocess
from pathlib import Path

from labstack.environment import shell_environment
from labstack.healthcheck import wait_for_health
from labstack.inventory import nodes, primary_node
from labstack.logger import get_logger
from labstack.models.config import Config
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.planning import prepare_plan, provision_graph
from labstack.settings import KUBERNETES_VERSION, get_settings
from labstack.ssh import remote, ssh_command

logger = get_logger(__name__)


def script_path(filename: str) -> Path:
    """Resolve a provisioning script and keep it inside the project boundary.

    Checking the resolved path also rejects symlinks that point outside provision/.
    The inventory's relative-path validation catches syntax errors; this check catches
    missing files or unsafe filesystem changes made after the inventory was loaded.
    """
    settings = get_settings()
    # Resolve symlinks before checking the boundary; a valid-looking inventory path
    # must not allow a link to run a script from another directory.
    path = (settings.root / filename).resolve()
    if (
        not path.is_relative_to((settings.root / "provision").resolve())
        or not path.is_file()
    ):
        raise ValueError(f"Unavailable provisioning script: {filename}")
    return path


def validate_scripts(config: Config) -> None:
    """Check every active script before any provisioning command is run.

    A missing script on a worker must not be discovered after the first machine has
    already been changed. No-provision startup intentionally skips this file check,
    allowing infrastructure practice without requiring workload recipes.
    """
    for node in nodes(config):
        for filename in node.scripts:
            script_path(filename)


def sync_shared(node: Node, *, download: bool = False) -> None:
    """Exchange private recipe files between guests through the host.

    The shared directory can contain join credentials. Host and guest permissions are
    kept private, and rsync preserves those modes in either direction. Files from an
    earlier script remain available to later rounds; this transfer does not delete them.
    Copy failures retain the node log and stop the provisioning round.
    """
    settings = get_settings()
    directory = settings.state_dir / "shared"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    # Set source permissions directly: macOS openrsync does not reliably apply
    # --chmod to regular files. Rsync then preserves the private modes at both ends.
    for path in directory.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    guest = "/var/lib/labstack/shared"
    remote(
        node,
        f"sudo install -d -m 0700 {guest} && "
        f"sudo find {guest} -type d -exec chmod 0700 {{}} + && "
        f"sudo find {guest} -type f -exec chmod 0600 {{}} +",
    )
    source, destination = f"{directory}/", f"{node.name}:{guest}/"
    if download:
        source, destination = destination, source
    with (settings.logs_dir / f"{node.name}-provision.log").open("a") as stream:
        result = subprocess.run(
            [
                "rsync",
                "-rtp",
                "--checksum",
                "--rsync-path=sudo rsync",
                "-e",
                shlex.join(["ssh", "-F", str(settings.state_dir / "ssh_config")]),
                source,
                destination,
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    if result.returncode:
        raise ValueError(
            f"Shared files could not be copied for {node.name}. See its log."
        )


def run_script(node: Node, filename: str, state: State) -> None:
    """Run one validated root script with its machine context and a durable log.

    The host sends script contents over SSH instead of requiring a guest mount. Node
    values are shell-quoted before being passed to sudo env. Output is streamed to a
    file so interruption or failure still leaves useful diagnostics; credentials in
    shared files are not added to the structured application log.
    """
    settings = get_settings()
    # Load user values inside the root shell; sudo would otherwise discard them.
    script = (
        shell_environment(state.environment_for(node.name))
        + script_path(filename).read_text()
    )
    environment = {
        "NODE_NAME": node.name,
        # Scripts run as root, so their login account must be passed explicitly.
        "SSH_USER": node.ssh_user,
        "NODE_IP": node.address,
        "PRIMARY_NODE_IP": primary_node(state.config).address,
        "NODE_COUNT": str(len(nodes(state.config))),
        "KUBERNETES_VERSION": KUBERNETES_VERSION,
        "SHARED_DIR": "/var/lib/labstack/shared",
    }
    command = shlex.join(
        [
            "sudo",
            "env",
            *[f"{key}={value}" for key, value in environment.items()],
            "bash",
            "-s",
        ]
    )
    with (settings.logs_dir / f"{node.name}-provision.log").open("a") as stream:
        stream.write(f"\nRunning {filename}\n")
        stream.flush()
        # Write as the guest works, so an interrupted installation still has useful logs.
        result = subprocess.run(
            ssh_command(node, command),
            input=script,
            text=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    if result.returncode:
        raise ValueError(f"{filename} failed on {node.name}. See its provisioning log.")


def provision(state: State) -> None:
    """Run ready scripts in batches, requiring each dependency checkpoint to succeed.

    Without dependencies, batches retain the inventory's round-by-round behavior.
    Explicit gates can wait for a server with a longer list or release clients before
    its addons. Files are synchronized around each successful script. A failed script
    or healthcheck stops the remaining work and leaves logs and disks for inspection.
    """
    validate_scripts(state.config)
    machines = {node.name: node for node in nodes(state.config)}
    plan = prepare_plan(state.config.active_machines)
    order = {
        step: index
        for index, step in enumerate(provision_graph(state.config.active_machines))
    }
    while plan.is_active():
        # Keep YAML order within a ready batch. Dependency sets alone do not define order.
        for step in sorted(plan.get_ready(), key=order.__getitem__):
            node = machines[step.machine]
            if step.kind == "healthcheck":
                wait_for_health(node, state)
            elif step.kind == "script":
                filename = node.scripts[step.index]
                logger.info(
                    "Running provisioning script.", node=node.name, script=filename
                )
                sync_shared(node)
                run_script(node, filename, state)
                sync_shared(node, download=True)
            plan.done(step)
