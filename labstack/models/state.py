"""Record the image pins and machine identity needed to resume an environment."""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labstack.models.config import Config
from labstack.models.environment import Environment
from labstack.models.image import Box


class State(BaseModel):
    """Record enough information to restart or recreate the same environment.

    The inventory preserves machine choices, resolved boxes preserve release pins, and
    base paths preserve disk selection. The instance identity survives a normal restart;
    a reset creates a new identity and new overlays while reusing the same base images.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    config: Config
    instance_id: str
    base_images: dict[str, Path]
    resolved_boxes: dict[str, Box] = Field(default_factory=dict)
    environments: dict[str, Environment] = Field(default_factory=dict)
    provisioned: bool = False
    # Existing records lack this field. New labs set it explicitly so recipe output
    # under .state/shared is never mistaken for an older VM named shared.
    resource_layout: Literal["legacy", "namespaced"] = "legacy"

    def environment_for(self, name: str) -> dict[str, str]:
        """Use saved file values; older records can only contain inline variables."""
        return self.environments.get(name, self.config.machines[name].environment)

    @model_validator(mode="after")
    def validate_environments(self) -> Self:
        """Require a complete snapshot when resolved environments have been saved."""
        if self.environments and set(self.environments) != set(
            self.config.active_machines
        ):
            raise ValueError("Saved environments must match the active machines.")
        return self

    @property
    def kubernetes_enabled(self) -> bool:
        """Run automatic Kubernetes checks only after provisioning has completed."""
        return self.provisioned and self.config.kubernetes_enabled

    @model_validator(mode="after")
    def validate_base_images(self) -> Self:
        """Reject state that assigns missing or unrelated image pins to a machine.

        Published metadata must cover all active machines and match their references.
        A local IMAGE_PATH override has no published metadata, so an empty pin map is valid.
        """
        if set(self.base_images) != set(self.config.active_machines):
            raise ValueError("Saved base images must match the active machines.")
        # Local QCOW2 overrides have no release metadata. Published boxes cover every node.
        if self.resolved_boxes:
            if set(self.resolved_boxes) != set(self.base_images):
                raise ValueError("Saved box metadata must match the active machines.")
            for name, machine in self.config.active_machines.items():
                pin = self.resolved_boxes[name]
                if (
                    pin.target != machine.box.target
                    or pin.ssh_user != machine.ssh_user
                    or (
                        machine.box.version and pin.release != f"v{machine.box.version}"
                    )
                ):
                    raise ValueError(
                        "Saved box metadata must match its machine reference."
                    )
        return self
