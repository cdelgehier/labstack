"""Run Labstack commands with a lifecycle lock and useful failure logs."""

import argparse
import fcntl
import os
import shlex
import subprocess

from labstack.cluster import destroy, down, read_state, reset, up
from labstack.inventory import nodes, primary_node, select_node
from labstack.kubernetes import export_kubeconfig, prepare_client
from labstack.logger import configure_logging, get_logger
from labstack.qemu import running
from labstack.settings import get_settings
from labstack.ssh import remote, ssh_command

logger = get_logger(__name__)


def main() -> None:
    """Dispatch one infrastructure command while protecting lifecycle mutations.

    The project lock prevents overlapping starts, resets or cleanup from changing the
    same disks. Read-only status and guest connections remain available during a long
    provisioning run. Task environment options are validated by Settings, while the
    saved inventory controls existing machines. Command output stays on stdout.
    """
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="labstack", description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "up",
            "down",
            "destroy",
            "reset",
            "status",
            "ssh",
            "exec",
            "kubeconfig",
            "test",
            "client",
            "kubectl",
        ],
    )
    parser.add_argument("--node", help="Exact machine name from the saved inventory.")
    parser.add_argument("--command")
    parser.add_argument("--no-provision", action="store_true")
    args = parser.parse_args()
    if args.no_provision and args.action != "up":
        parser.error("--no-provision is only available with up.")
    if args.action in {"ssh", "exec"} and not args.node:
        parser.error("Provide a machine name with --node (for example, --node cp).")
    # Keys, kubeconfig and shared recipe files must stay private from their first write.
    os.umask(0o077)
    settings.logs_dir.mkdir(mode=0o700, exist_ok=True)
    settings.logs_dir.chmod(0o700)
    with (settings.root / ".cluster.lock").open("w") as lock:
        # Connections and status can inspect a long provisioning run without mutating disks.
        if args.action not in {"ssh", "exec", "status"}:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(
                    "Another lifecycle task is running in this folder."
                ) from error
        if args.action == "up":
            up(settings.profiles, settings.image_path, no_provision=args.no_provision)
            return
        if args.action in {"client", "kubectl"}:
            client = prepare_client()
            if args.action == "kubectl":
                arguments = shlex.split(settings.kubectl_args)
                subprocess.run(
                    [
                        str(client),
                        f"--kubeconfig={settings.root / 'kubeconfig'}",
                        *arguments,
                    ],
                    check=True,
                )
            return
        if args.action == "reset":
            reset()
            return
        if args.action == "destroy":
            destroy()
            return
        state = read_state()
        if args.action == "down":
            down(state)
        elif args.action == "status":
            print(
                f"PROFILES={','.join(state.config.profiles) or '(default)'}, provisioned={state.provisioned}"
            )
            for node in nodes(state.config):
                print(
                    f"{node.name}: {'running' if running(node, state) else 'stopped'} "
                    f"({node.address}, SSH 127.0.0.1:{node.ssh_port})"
                )
            if state.kubernetes_enabled and running(primary_node(state.config), state):
                print(remote(primary_node(state.config), "kubectl get nodes -o wide"))
        elif args.action in {"ssh", "exec"}:
            node = select_node(state.config, args.node)
            if args.action == "exec":
                print(remote(node, args.command or "true"), end="")
            else:
                subprocess.run(ssh_command(node), check=True)
        elif args.action == "kubeconfig":
            export_kubeconfig(state)
        elif args.action == "test":
            from tests.runtime import check_environment

            check_environment(state)


def entrypoint() -> None:
    """Configure logs and return useful exit codes for Task and shell callers.

    Interrupted builds keep their VM state for inspection or a later retry. Expected
    validation and command errors produce one log event; guest output stays in the
    diagnostic files rather than being mixed into the terminal error message.
    """
    # Set up a default logger first so invalid environment settings also have a clear error.
    configure_logging()
    try:
        configure_logging(get_settings().log_level)
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        logger.error("Command failed.", error=str(error))
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        logger.warning(
            "Interrupted. VM state was kept; use 'task status' or 'task down'."
        )
        raise SystemExit(130) from None
