"""Run bounded guest readiness checks and retain output for failed startup attempts."""

import shlex
import subprocess
import time

from labstack.environment import shell_environment
from labstack.logger import get_logger
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.qemu import running
from labstack.settings import get_settings
from labstack.ssh import ssh_command

logger = get_logger(__name__)


def wait_for_health(node: Node, state: State) -> None:
    """Require one successful guest command before releasing its dependent scripts.

    Each attempt has its own SSH timeout and its output goes directly to a private log.
    A stopped guest fails promptly. These are startup checks, not background monitoring;
    another task up runs them again along with the scripts.
    """
    check = node.healthcheck
    assert check is not None
    command = shlex.join(check.test[1:]) if check.test[0] == "CMD" else check.test[1]
    script = shell_environment(state.environment_for(node.name)) + command + "\n"
    log = get_settings().logs_dir / f"{node.name}-healthcheck.log"
    logger.info("Checking guest health.", node=node.name)
    if check.start_period:
        time.sleep(check.start_period)
    with log.open("a") as stream:
        for attempt in range(1, check.retries + 1):
            if not running(node, state):
                stream.write("Guest stopped before the healthcheck completed.\n")
                raise ValueError(
                    f"{node.name} stopped during its healthcheck. See {log}."
                )
            stream.write(f"\nAttempt {attempt}/{check.retries}\n")
            stream.flush()
            try:
                result = subprocess.run(
                    ssh_command(node, "bash -s"),
                    input=script,
                    text=True,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=check.timeout,
                )
            except subprocess.TimeoutExpired as error:
                stream.write(f"Command timed out after {check.timeout:g}s.\n")
                if error.output:
                    output = (
                        error.output.decode(errors="replace")
                        if isinstance(error.output, bytes)
                        else error.output
                    )
                    stream.write(output)
            else:
                if result.returncode == 0:
                    logger.info("Guest healthcheck passed.", node=node.name)
                    return
                stream.write(f"Exit status: {result.returncode}\n")
            stream.flush()
            if attempt < check.retries:
                time.sleep(check.interval)
    raise ValueError(
        f"{node.name} healthcheck failed after {check.retries} attempts. See {log}."
    )
