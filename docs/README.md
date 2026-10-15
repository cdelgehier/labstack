# Labstack documentation

Start with the [project README](../README.md) to create your first lab.
Run all commands from the project directory.

| Guide | What you will find |
| --- | --- |
| [Configuration](configuration.md) | Machines, image versions, networks, ports and provisioning scripts |
| [Guest environment](environment.md) | Per-machine variables, dotenv files and systemd services |
| [Inventory without YAML anchors](explicit-inventory.md) | A complete example with every VM written out and lists expanded |
| [Usage](usage.md) | Manual installation, guest access, lifecycle commands and disk locations |
| [Development](development.md) | Python package layout, settings, logging and checks |

The included recipe creates a Kubernetes 1.37.1 cluster on Ubuntu 26.04 ARM64.
For Argo CD, Crossplane or another workload, add your own scripts or install the
tools inside the running lab.
