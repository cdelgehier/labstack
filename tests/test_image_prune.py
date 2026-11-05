"""Remove unused cached disks without changing saved labs or reset choices."""

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from labstack import pruning
from labstack.inventory import read_config
from labstack.models.state import State
from labstack.settings import get_settings


@pytest.fixture
def cache_environment(tmp_path, monkeypatch):
    """Keep all prune paths inside a disposable project."""
    settings = get_settings()
    for field, path in {
        "root": tmp_path,
        "state_dir": tmp_path / ".state",
        "cache_dir": tmp_path / ".cache",
        "logs_dir": tmp_path / "logs",
    }.items():
        monkeypatch.setattr(settings, field, path)
    settings.cache_dir.mkdir()
    return settings


def test_prune_removes_only_regular_labstack_base_images(cache_environment):
    cache = cache_environment.cache_dir
    unused = cache / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"unused cached disk")
    unused.chmod(0o444)
    for name in ("kubectl-1.37.1", "custom.qcow2", "base-short.qcow2", "box.part"):
        (cache / name).write_bytes(b"keep unrelated files")
    outside = cache_environment.root / "external.qcow2"
    outside.write_bytes(b"keep outside image")
    link = cache / f"base-{'b' * 64}.qcow2"
    link.symlink_to(outside)
    directory = cache / f"base-{'c' * 64}.qcow2"
    directory.mkdir()

    assert pruning.prune_images() == [unused]

    assert not unused.exists()
    assert outside.read_bytes() == b"keep outside image"
    assert link.is_symlink()
    assert directory.is_dir()
    assert (cache / "kubectl-1.37.1").read_bytes() == b"keep unrelated files"
    assert (cache / "custom.qcow2").exists()
    assert (cache / "base-short.qcow2").exists()
    assert (cache / "box.part").exists()


def test_dry_run_lists_unused_images_without_deleting_them(cache_environment):
    unused = cache_environment.cache_dir / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"unchanged disk")

    assert pruning.prune_images(dry_run=True) == [unused]

    assert unused.read_bytes() == b"unchanged disk"


@pytest.mark.parametrize("record", [".state/state.json", ".reset.json"])
def test_saved_labs_and_pending_resets_keep_their_base_images(
    cache_environment, kubernetes_inventory, record
):
    settings = cache_environment
    used = settings.cache_dir / f"base-{'a' * 64}.qcow2"
    unused = settings.cache_dir / f"base-{'b' * 64}.qcow2"
    used.write_bytes(b"saved image")
    unused.write_bytes(b"old image")
    config = read_config(kubernetes_inventory)
    state = State(
        config=config,
        instance_id="saved",
        base_images=dict.fromkeys(config.active_machines, used),
    )
    state_file = settings.root / record
    state_file.parent.mkdir(exist_ok=True)
    state_file.write_text(state.model_dump_json())
    original_state = state_file.read_bytes()

    assert pruning.prune_images() == [unused]

    assert used.read_bytes() == b"saved image"
    assert state_file.read_bytes() == original_state


def test_state_and_reset_can_protect_different_images(
    cache_environment, kubernetes_inventory
):
    settings = cache_environment
    config = read_config(kubernetes_inventory)
    used = []
    for record, digest in ((".state/state.json", "a"), (".reset.json", "b")):
        image = settings.cache_dir / f"base-{digest * 64}.qcow2"
        image.write_bytes(b"keep for saved or pending lab")
        used.append(image)
        state = State(
            config=config,
            instance_id="saved",
            base_images=dict.fromkeys(config.active_machines, image),
        )
        path = settings.root / record
        path.parent.mkdir(exist_ok=True)
        path.write_text(state.model_dump_json())
    unused = settings.cache_dir / f"base-{'c' * 64}.qcow2"
    unused.write_bytes(b"unused")

    assert pruning.prune_images() == [unused]
    assert all(image.exists() for image in used)


def test_relative_saved_image_paths_are_resolved_from_the_project(
    cache_environment, kubernetes_inventory
):
    settings = cache_environment
    used = settings.cache_dir / f"base-{'a' * 64}.qcow2"
    used.write_bytes(b"saved image")
    config = read_config(kubernetes_inventory)
    state = State(
        config=config,
        instance_id="saved",
        base_images=dict.fromkeys(
            config.active_machines, used.relative_to(settings.root)
        ),
    )
    settings.reset_file.write_text(state.model_dump_json())

    assert pruning.prune_images() == []
    assert used.exists()


@pytest.mark.parametrize("record", [".state/state.json", ".reset.json"])
def test_broken_state_links_stop_pruning(cache_environment, record):
    unused = cache_environment.cache_dir / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"keep until state can be read")
    path = cache_environment.root / record
    path.parent.mkdir(exist_ok=True)
    path.symlink_to(cache_environment.root / "missing-state.json")

    with pytest.raises(ValueError, match="Unreadable saved image references"):
        pruning.prune_images()

    assert unused.exists()


@pytest.mark.parametrize("record", [".state/state.json", ".reset.json"])
@pytest.mark.parametrize("content", ["not JSON", '{"base_images": {}}'])
def test_unreadable_state_stops_pruning_before_any_image_is_removed(
    cache_environment, record, content
):
    unused = cache_environment.cache_dir / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"keep while references are unknown")
    path = cache_environment.root / record
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)

    with pytest.raises(ValueError):
        pruning.prune_images()

    assert unused.read_bytes() == b"keep while references are unknown"


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
@pytest.mark.parametrize("layout", ["vm", "machines/vm"])
def test_prune_keeps_actual_relative_backing_chains_without_saved_state(
    cache_environment, layout
):
    settings = cache_environment
    used = settings.cache_dir / f"base-{'a' * 64}.qcow2"
    unused = settings.cache_dir / f"base-{'b' * 64}.qcow2"
    unused.write_bytes(b"unused disk")
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", str(used), "64M"], check=True
    )
    intermediate = settings.state_dir / "middle.qcow2"
    disk = settings.state_dir / layout / "disk.qcow2"
    disk.parent.mkdir(parents=True)
    for image, backing in ((intermediate, used), (disk, intermediate)):
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-q",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                os.path.relpath(backing, image.parent),
                image.name,
            ],
            cwd=image.parent,
            check=True,
        )
    before = used.read_bytes()

    assert pruning.prune_images() == [unused]

    assert used.read_bytes() == before
    subprocess.run(["qemu-img", "check", "-q", str(disk)], check=True)


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
def test_invalid_guest_disk_stops_pruning_before_deletion(cache_environment):
    unused = cache_environment.cache_dir / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"keep until disk references can be read")
    disk = cache_environment.state_dir / "machines/vm/disk.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"invalid QCOW2 disk")

    with pytest.raises(ValueError, match="QCOW2"):
        pruning.prune_images()

    assert unused.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_shared_or_external_caches_are_not_pruned(
    cache_environment, dry_run, monkeypatch
):
    shared = cache_environment.root / "shared-cache"
    shared.mkdir()
    image = shared / f"base-{'a' * 64}.qcow2"
    image.write_bytes(b"another lab may use this")
    monkeypatch.setattr(cache_environment, "cache_dir", shared)

    with pytest.raises(ValueError, match="project.*cache"):
        pruning.prune_images(dry_run=dry_run)

    assert image.exists()


def test_an_absent_cache_is_a_noop(cache_environment):
    cache_environment.cache_dir.rmdir()

    assert pruning.prune_images() == []

    assert not cache_environment.cache_dir.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="Root can read restricted directories")
def test_unreadable_vm_directories_stop_pruning(cache_environment):
    unused = cache_environment.cache_dir / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"keep when disk references cannot be inspected")
    directory = cache_environment.state_dir / "machines/vm"
    directory.mkdir(parents=True)
    (directory / "disk.qcow2").write_bytes(b"unreadable disk")
    directory.chmod(0)
    try:
        with pytest.raises(PermissionError):
            pruning.prune_images()
        assert unused.exists()
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_prunes_without_an_inventory_or_vm_state(tmp_path, dry_run):
    cache = tmp_path / ".cache"
    cache.mkdir()
    unused = cache / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"unused image")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "labstack",
            "images-prune",
            *(["--dry-run"] if dry_run else []),
        ],
        env=dict(
            os.environ,
            LABSTACK_ROOT=str(tmp_path),
            LABSTACK_CACHE_DIR=".cache",
            PROFILES="",
            IMAGE_PATH="",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert str(unused) in result.stdout
    assert unused.exists() is dry_run
    assert not (tmp_path / ".state").exists()


def test_cli_refuses_prune_during_another_lifecycle_task(tmp_path):
    cache = tmp_path / ".cache"
    cache.mkdir()
    unused = cache / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"keep while another task is active")
    with (tmp_path / ".cluster.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-m", "labstack", "images-prune"],
            env=dict(
                os.environ,
                LABSTACK_ROOT=str(tmp_path),
                LABSTACK_CACHE_DIR=".cache",
                PROFILES="",
                IMAGE_PATH="",
            ),
            capture_output=True,
            text=True,
        )

    assert result.returncode == 1
    assert "Another lifecycle task" in result.stderr
    assert unused.exists()


@pytest.mark.skipif(not shutil.which("task"), reason="Task is not installed")
@pytest.mark.parametrize(
    ("task_name", "arguments", "dry_run"),
    [
        ("images:prune", [], False),
        ("images:prune:dry-run", [], True),
        ("images:prune", ["--", "--dry-run"], True),
    ],
)
def test_task_runs_the_matching_prune_mode_in_a_disposable_project(
    tmp_path, task_name, arguments, dry_run
):
    cache = tmp_path / ".cache"
    cache.mkdir()
    unused = cache / f"base-{'a' * 64}.qcow2"
    unused.write_bytes(b"unused image")
    result = subprocess.run(
        [
            "task",
            "--dir",
            str(Path(__file__).resolve().parents[1]),
            task_name,
            *arguments,
        ],
        env=dict(
            os.environ,
            LABSTACK_ROOT=str(tmp_path),
            LABSTACK_CACHE_DIR=".cache",
            PROFILES="",
            IMAGE_PATH="",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert str(unused) in result.stdout
    assert unused.exists() is dry_run
