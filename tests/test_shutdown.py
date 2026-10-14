"""Keep slow guest shutdowns graceful without waiting on a real VM or clock."""

import pytest

from labstack import cluster
from labstack.inventory import read_config
from labstack.models.machine import Node
from labstack.models.state import State


class GuestShutdownFake:
    """Model a guest that stops after its shutdown work, or only after QEMU quits.

    Virtual time lets the tests cover Ubuntu's 90-second container stop timeout
    without making the test suite wait. Each guest gets its own shutdown deadline.
    """

    def __init__(self, duration: float):
        self.duration = duration
        self.elapsed = 0.0
        self.stop_times: dict[str, float] = {}
        self.powered_down: list[str] = []
        self.forced: list[str] = []

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds

    def running(self, node: Node, state: State) -> bool:
        return self.elapsed < self.stop_times.get(node.name, float("inf"))

    def qmp(self, node: Node, command: str, state: State) -> None:
        if command == "system_powerdown":
            self.powered_down.append(node.name)
            self.stop_times[node.name] = self.elapsed + self.duration
        elif command == "quit":
            self.forced.append(node.name)
            self.stop_times[node.name] = self.elapsed
        else:
            raise ValueError(f"Unexpected monitor command: {command}")


@pytest.mark.parametrize("duration", [70, 90])
def test_slow_guest_shutdown_finishes_before_qemu_is_forced(
    tmp_path, monkeypatch, duration
):
    state = State(
        config=read_config(),
        instance_id="test",
        base_images=dict.fromkeys(("cp", "w1"), tmp_path / "base"),
    )
    guest = GuestShutdownFake(duration)
    monkeypatch.setattr(cluster, "time", guest)
    monkeypatch.setattr(cluster, "running", guest.running)
    monkeypatch.setattr(cluster, "qmp", guest.qmp)

    cluster.down(state)

    assert guest.powered_down == ["w1", "cp"]
    assert guest.forced == []


def test_a_guest_that_never_shuts_down_still_has_a_bounded_wait(tmp_path, monkeypatch):
    state = State(
        config=read_config(),
        instance_id="test",
        base_images=dict.fromkeys(("cp", "w1"), tmp_path / "base"),
    )
    guest = GuestShutdownFake(float("inf"))
    monkeypatch.setattr(cluster, "time", guest)
    monkeypatch.setattr(cluster, "running", guest.running)
    monkeypatch.setattr(cluster, "qmp", guest.qmp)

    cluster.down(state)

    assert guest.powered_down == ["w1", "cp"]
    assert guest.forced == ["w1", "cp"]
    assert guest.elapsed < 300
