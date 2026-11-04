# Inventory backups

`machines.yml` is the active inventory and the file to edit. The other files are
saved copies:

- `machines.yml.cks`: Kubernetes lab.
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

The Holmes backup installs the HTTP service, probes, host metrics, Prometheus,
Alertmanager, Grafana, a demo data volume and a local notification inbox. It also
adds a traced API, Loki logs, Jaeger traces and an OpenTelemetry collector. See the
[observability guide](../provision/observability/README.md) to run the demo.
[HolmesGPT](../provision/holmesGPT/README.md) investigates alerts, uses separate
SOPs and reports guarded repairs or escalations through ntfy.
