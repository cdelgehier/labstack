"""Order guest scripts and readiness checkpoints without depending on an OS recipe."""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Literal

from labstack.models.machine import Machine


@dataclass(frozen=True)
class ProvisionStep:
    """Identify one script, healthcheck or completion marker in the setup graph."""

    machine: str
    kind: Literal["script", "healthcheck", "completed"]
    index: int = 0


def provision_graph(
    machines: dict[str, Machine],
) -> dict[ProvisionStep, set[ProvisionStep]]:
    """Build per-guest script chains and add the cross-guest dependency gates.

    A guest healthcheck is a separate checkpoint, not always the end of its setup.
    This lets a server open its API, clients register, and server addons run last.
    Started dependencies need no edge: every active guest has already passed SSH and
    cloud-init checks before provisioning begins. Completion markers also cover guests
    with empty script lists.
    """
    graph: dict[ProvisionStep, set[ProvisionStep]] = {}
    for name, machine in machines.items():
        previous: ProvisionStep | None = None
        for index, _ in enumerate(machine.scripts):
            step = ProvisionStep(name, "script", index)
            graph[step] = {previous} if previous else set()
            previous = step
            health = machine.healthcheck
            if health and health.after == machine.scripts[index]:
                check = ProvisionStep(name, "healthcheck")
                graph[check] = {step}
                previous = check
        if machine.healthcheck and machine.healthcheck.after is None:
            check = ProvisionStep(name, "healthcheck")
            graph[check] = {previous} if previous else set()
            previous = check
        graph[ProvisionStep(name, "completed")] = {previous} if previous else set()

    for name, machine in machines.items():
        for dependency_name, dependency in machine.depends_on.items():
            if dependency_name not in machines:
                if dependency.required:
                    raise ValueError(
                        f"{name} requires inactive machine {dependency_name}."
                    )
                continue
            if dependency.condition == "started":
                continue
            index = machine.scripts.index(dependency.before) if dependency.before else 0
            if machine.scripts:
                gate = ProvisionStep(name, "script", index)
            elif machine.healthcheck:
                # With no scripts, checking the service is the first guest action.
                gate = ProvisionStep(name, "healthcheck")
            else:
                gate = ProvisionStep(name, "completed")
            kind = "healthcheck" if dependency.condition == "healthy" else "completed"
            graph[gate].add(ProvisionStep(dependency_name, kind))
    return graph


def prepare_plan(machines: dict[str, Machine]) -> TopologicalSorter[ProvisionStep]:
    """Reject setup cycles and return a ready-to-run graph for active machines.

    Cycle errors name the participating guests. Validation calls this before any image
    download, so an impossible dependency chain never leaves a half-created lab.
    """
    plan = TopologicalSorter(provision_graph(machines))
    try:
        plan.prepare()
    except CycleError as error:
        names = ", ".join(dict.fromkeys(step.machine for step in error.args[1]))
        raise ValueError(f"Provisioning dependency cycle between: {names}.") from error
    return plan
