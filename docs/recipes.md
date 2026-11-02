# Inventory backups

`machines.yml` is the active inventory. The other files are saved copies:

- `machines.yml.cks`: Kubernetes lab.
- `machines.yml.holmes`: HolmesGPT demo lab.

To use a saved copy, copy it over `machines.yml`, then run the usual tasks.
If a lab already exists, run `task destroy` before switching.

```sh
cp machines.yml.holmes machines.yml
task up
```

To return to Kubernetes, copy `machines.yml.cks` over `machines.yml`.

## Provisioning layout

```text
provision/
├── base/             # Common recipes
├── kubernetes/       # Kubernetes recipes
├── holmesGPT/        # Holmes, SOPs and tools
└── observability/    # Target services and monitoring
```

The Holmes backup currently references only the common packages. The next steps
are described in [observability](../provision/observability/README.md) and
[HolmesGPT](../provision/holmesGPT/README.md).
