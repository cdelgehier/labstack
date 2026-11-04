# Example labs and inventory backups

Each lab defines its own machines, software and provisioning scripts. Labstack
creates and manages the VMs described by the selected inventory.

| Lab | Saved inventory | Guide |
| --- | --- | --- |
| CKS practice | [machines.yml.cks](../machines.yml.cks) | [Kubernetes lab](../provision/kubernetes/README.md) |
| HolmesGPT demo | [machines.yml.holmes](../machines.yml.holmes) | [Holmes investigations](../provision/holmesGPT/README.md) and [observability](../provision/observability/README.md) |

The Holmes lab also has a [presentation](presentations/holmesgpt/README.md) with
speaker notes and live demo scenarios.

## Select or save an inventory

`machines.yml` is the active inventory and the file to edit. The other files are
saved copies:

- `machines.yml.cks`: CKS Kubernetes lab.
- `machines.yml.holmes`: HolmesGPT demo lab.

To use a saved copy, copy it over `machines.yml`, then run the usual tasks.
If a lab already exists, run `task destroy` before switching.

```sh
cp machines.yml.holmes machines.yml
task up
```

To return to Kubernetes, copy `machines.yml.cks` over `machines.yml`.
When a recipe is ready, save it with `cp machines.yml machines.yml.holmes` (or
`machines.yml.cks` for Kubernetes).

## Provisioning layout

```text
provision/
├── base/             # Common recipes
├── kubernetes/       # Kubernetes recipes
├── holmesGPT/        # Holmes, SOPs and tools
└── observability/    # Target services and monitoring
```

Add a directory under `provision/` for another lab and reference its scripts from
your inventory. The lab guides describe the installed software and its exercises.
