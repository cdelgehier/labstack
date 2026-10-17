"""Check package lock handling without installing packages on the test host."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(
    params=["provision/base/packages.sh", "provision/kubernetes/prepare.sh"],
    ids=["base", "kubernetes"],
)
def apt_helper(request):
    """Read only the helper so tests never run the recipe's system changes.

    Both recipes must also work when copied to a guest on their own. Testing each
    copy keeps their lock handling aligned without adding a shared guest library.
    """
    recipe = (Path(__file__).resolve().parents[1] / request.param).read_text()
    start = recipe.index("apt_run() (")
    end = recipe.index("\n)", start) + 2
    return recipe[start:end]


@pytest.fixture
def apt_environment(tmp_path):
    """Replace package commands and sleep while keeping the real Bash control flow."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Shell builtins avoid starting Python for every package command attempt.
    package_fake = """#!/bin/bash
calls=0
if [[ -f "$APT_CALLS" ]]; then
  while IFS= read -r line; do calls=$((calls + 1)); done <"$APT_CALLS"
fi
printf '%s\\t%s\\t%s\\n' "${0##*/}" "$LC_ALL" "$*" >>"$APT_CALLS"
echo 'Package command output'
if ((calls < ${APT_FAILURES:-0})); then
  echo "$APT_ERROR" >&2
  exit "${APT_STATUS:-100}"
fi
echo 'Package command completed'
"""
    for command in ("apt-get", "apt-mark"):
        (binaries / command).write_text(package_fake)
    (binaries / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for binary in binaries.iterdir():
        binary.chmod(0o700)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    return dict(
        os.environ,
        PATH=f"{binaries}:{os.environ['PATH']}",
        TMPDIR=str(temporary),
        APT_CALLS=str(tmp_path / "calls.tsv"),
        LC_ALL="fr_FR.UTF-8",
    )


@pytest.mark.parametrize(
    "command, error",
    [
        (
            "apt-get update -qq",
            "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 123 (apt-get)",
        ),
        (
            "apt-get install -y curl",
            "E: Could not get lock /var/cache/apt/archives/lock. It is held by process 123",
        ),
        (
            "apt-get install -y curl",
            "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 123 (unattended-upgr)",
        ),
        (
            "apt-get update -qq",
            "E: Could not get lock /var/lib/apt/lists/lock - open (11: Resource temporarily unavailable)",
        ),
        (
            "apt-get install -y curl",
            "Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 123 (apt-get)\nE: Unable to acquire the dpkg frontend lock",
        ),
        (
            "apt-mark hold kubelet",
            "dpkg: error: dpkg database lock was locked by /usr/bin/dpkg process with pid 123",
        ),
        (
            "apt-mark hold kubelet",
            "dpkg: error: dpkg frontend lock was locked by another process with pid 123",
        ),
    ],
    ids=[
        "lists",
        "archives",
        "dpkg-frontend",
        "lists-no-pid",
        "dpkg-native-wait",
        "apt-mark-database",
        "apt-mark-frontend",
    ],
)
def test_package_command_waits_for_a_busy_lock_then_succeeds(
    apt_helper, apt_environment, command, error
):
    apt_environment.update(APT_FAILURES="2", APT_ERROR=error)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail\n{apt_helper}\napt_run {command}\necho ready",
        ],
        env=apt_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout
    assert "Waiting for APT lock" in result.stderr
    assert result.stderr.count(error) == 2
    assert "Package command output" in result.stderr
    assert "Package command completed" in result.stderr
    calls = [
        line.split("\t")
        for line in Path(apt_environment["APT_CALLS"]).read_text().splitlines()
    ]
    assert len(calls) == 3
    assert all(call[0] == command.split()[0] for call in calls)
    assert all(call[1] == "C" for call in calls)
    assert list(Path(apt_environment["TMPDIR"]).iterdir()) == []


@pytest.mark.parametrize(
    "command",
    ["apt-get update -qq", "apt-mark hold kubelet"],
    ids=["apt-get", "apt-mark"],
)
@pytest.mark.parametrize(
    "error, status",
    [
        ("E: Unable to locate package missing", 100),
        (
            "E: Could not open lock file /var/lib/apt/lists/lock - open (13: Permission denied)",
            100,
        ),
        ("E: Unable to lock directory /var/lib/apt/lists/", 100),
        ("E: The repository does not have a Release file.", 100),
        ("E: Sub-process /usr/bin/dpkg returned an error code (1)", 100),
        ("E: Invalid command line option", 2),
    ],
    ids=[
        "missing-package",
        "permission",
        "unknown-lock",
        "repository",
        "broken-install",
        "invalid-option",
    ],
)
def test_other_package_errors_stop_provisioning_without_retry(
    apt_helper, apt_environment, command, error, status
):
    apt_environment.update(APT_FAILURES="1", APT_ERROR=error, APT_STATUS=str(status))
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail\n{apt_helper}\napt_run {command}\necho ready",
        ],
        env=apt_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == status
    assert error in result.stderr
    assert "ready" not in result.stdout
    assert "Waiting for APT lock" not in result.stderr
    assert len(Path(apt_environment["APT_CALLS"]).read_text().splitlines()) == 1
    assert list(Path(apt_environment["TMPDIR"]).iterdir()) == []


def test_a_log_capture_failure_is_not_reported_as_success(apt_helper, apt_environment):
    """A successful APT command must not hide a failure to save its diagnostics."""
    binaries = Path(apt_environment["APT_CALLS"]).parent / "bin"
    tee = binaries / "tee"
    tee.write_text("#!/bin/sh\ncat >/dev/null\nexit 74\n")
    tee.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{apt_helper}\napt_run apt-get update -q"],
        env=apt_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 74
    assert len(Path(apt_environment["APT_CALLS"]).read_text().splitlines()) == 1
    assert list(Path(apt_environment["TMPDIR"]).iterdir()) == []


def test_base_recipe_uses_lock_handling_for_its_package_commands(apt_environment):
    apt_environment.update(
        APT_FAILURES="1",
        APT_ERROR="E: Could not get lock /var/lib/apt/lists/lock. It is held by process 123",
    )
    recipe = Path(__file__).resolve().parents[1] / "provision/base/packages.sh"
    result = subprocess.run(
        ["bash", str(recipe)],
        env=apt_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "Waiting for APT lock" in result.stderr
    calls = [
        line.split("\t")[2].split()
        for line in Path(apt_environment["APT_CALLS"]).read_text().splitlines()
    ]
    assert len(calls) == 3
    assert "update" in calls[1]
    assert "install" in calls[2]
    # Double quiet hides native dpkg waits, so users lose the reason for a delay.
    assert all("-q" in call and "-qq" not in call for call in calls)


@pytest.mark.parametrize(
    "elapsed, expected_delays, expected_timeouts",
    [(95, [5, 5, 5], [300, 200, 100]), (296, [4], [300]), (300, [], [300])],
)
def test_retries_share_one_deadline_and_preserve_the_last_error(
    apt_helper, apt_environment, tmp_path, elapsed, expected_delays, expected_timeouts
):
    apt_environment.update(
        APT_FAILURES="100",
        APT_STATUS="42",
        APT_ERROR="E: Could not get lock /var/lib/apt/lists/lock. It is held by process 123 (apt-get)",
    )
    waits = tmp_path / "waits"
    # Only the clock is accelerated. Include time spent in APT itself, and check
    # that a final short sleep cannot take the loop past its five-minute budget.
    script = (
        f"set -euo pipefail\nunset SECONDS\nSECONDS=0\n{apt_helper}\n"
        # The pipeline runs in children. Advance the parent clock when it reads
        # their diagnostics, as if the package command had used this much time.
        f'grep() {{ SECONDS=$((SECONDS + {elapsed})); command grep "$@"; }}\n'
        f'sleep() {{ echo "$1" >>"{waits}"; SECONDS=$((SECONDS + $1)); }}\n'
        "apt_run apt-get update -qq\necho ready"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=apt_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 42
    assert "Timed out waiting for APT lock" in result.stderr
    assert "ready" not in result.stdout
    delays = (
        [int(line) for line in waits.read_text().splitlines()] if waits.exists() else []
    )
    assert delays == expected_delays
    calls = [
        line.split("\t")
        for line in Path(apt_environment["APT_CALLS"]).read_text().splitlines()
    ]
    timeouts = [int(call[2].split()[1].split("=")[1]) for call in calls]
    assert timeouts == expected_timeouts
    assert list(Path(apt_environment["TMPDIR"]).iterdir()) == []


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("apt-get") is None,
    reason="The POSIX lock test needs Linux and APT",
)
def test_real_apt_update_waits_for_an_isolated_posix_lock(apt_helper, tmp_path):
    """Use APT's real error text and lock protocol without network or installation."""
    import fcntl

    lists = tmp_path / "lists"
    lists.mkdir()
    sources = tmp_path / "sources.list"
    sources.touch()
    configuration_parts = tmp_path / "apt.conf.d"
    configuration_parts.mkdir()
    empty_configuration = tmp_path / "empty.conf"
    empty_configuration.touch()
    configuration = tmp_path / "apt.conf"
    # APT_CONFIG is read before the host configuration. Skip host hooks too,
    # since an update hook could otherwise change the system package cache.
    configuration.write_text(
        f'Dir::Etc::main "{empty_configuration}";\n'
        f'Dir::Etc::parts "{configuration_parts}";\n'
        f'Dir::Cache "{tmp_path / "cache"}";\n'
        f'Dir::Log "{tmp_path / "log"}";\n'
    )
    script = (
        f"set -euo pipefail\n{apt_helper}\n"
        # Unit tests check the five-second delay. Shorten only this real wait.
        "sleep() { command sleep 0.05; }\n"
        'apt_run apt-get -o "Dir::State::lists=$APT_LISTS" '
        '-o "Dir::Etc::sourcelist=$APT_SOURCES" '
        "-o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=false update\n"
    )
    environment = dict(
        os.environ,
        APT_CONFIG=str(configuration),
        APT_LISTS=str(lists),
        APT_SOURCES=str(sources),
    )
    log = tmp_path / "apt.log"
    with (lists / "lock").open("w") as lock, log.open("w") as output:
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with subprocess.Popen(
            ["bash", "-c", script], env=environment, stdout=output, stderr=output
        ) as process:
            try:
                deadline = time.monotonic() + 5
                while "Waiting for APT lock" not in log.read_text():
                    assert process.poll() is None, log.read_text()
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.01)
                fcntl.lockf(lock, fcntl.LOCK_UN)
                process.wait(timeout=10)
                assert process.returncode == 0, log.read_text()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    assert "Could not get lock" in log.read_text()
    assert "Timed out" not in log.read_text()
