# Labstack documentation

Start with the [project README](../README.md) to create your first lab.
Run all commands from the project directory.

| Guide | What you will find |
| --- | --- |
| [Inventory backups](recipes.md) | Restore a saved inventory and use the usual tasks |
| [Configuration](configuration.md) | Machines, image versions, networks, ports and provisioning scripts |
| [Guest environment](environment.md) | Per-machine variables, dotenv files and systemd services |
| [Inventory without YAML anchors](explicit-inventory.md) | A complete example with every VM written out and lists expanded |
| [Usage](usage.md) | Manual installation, guest access, lifecycle commands and disk locations |
| [Development](development.md) | Python package layout, settings, logging and checks |

The active recipe provides the [HTTP monitoring demo](../provision/observability/README.md).
The saved `machines.yml.cks` recipe creates a Kubernetes cluster with Cilium.
Edit `machines.yml` while working, then save the completed recipe in its backup.
