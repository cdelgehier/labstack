# HolmesGPT SOPs

Each procedure has its own `<name>/SKILL.md` file, with the same layout as
the K3s SOPs. Procedures are not embedded in the Python bridge or its YAML
configuration.

| Procedure | Purpose |
| --- | --- |
| [HTTP service](http-service/SKILL.md) | Investigate `DemoHttpUnavailable` and start only the stopped demo HTTP service when permitted. |
| [File permissions](file-permissions/SKILL.md) | Investigate `DemoApiUnavailable` and restore only the known demo file permission fault when permitted. |
| [CPU pressure](cpu-pressure/SKILL.md) | Investigate `DemoCpuBusy` and stop only the verified demo CPU stress unit when authorized; escalate other causes. |
| [Memory pressure](memory-pressure/SKILL.md) | Investigate `DemoMemoryLow`, distinguish low available RAM from a proven OOM and request human intervention. |
| [Disk space](disk-space/SKILL.md) | Investigate `DemoDiskSpaceLow` on the affected filesystem without deleting data. |
| [Missing mount](missing-mount/SKILL.md) | Investigate `DemoExpectedMountMissing` without confusing root filesystem usage with demo-volume capacity. |
| [Monitoring](monitoring/SKILL.md) | Investigate failed scrapes or missing probes without treating missing telemetry as proof of an application outage. |

## Loading in the VM

Provisioning copies this directory to `/etc/labstack-holmes/sops`. The Holmes
container mounts it read-only at `/etc/holmes/sops`.
[The configuration renderer](../render_config.py) registers that directory
through Holmes's native `custom_skill_paths` setting. The procedures are always
available; the model chooses a relevant procedure from the native skill catalogue.

Holmes gives the model each procedure's `name` and `description` from the YAML
front matter. The model requests `fetch_skill` to load a relevant procedure's
body. There is no alert-to-SOP mapping in the bridge. If no procedure fits,
the model investigates with the available read tools and reports the gap.
SOPs guide the investigation;
the executor checks whether an action is permitted. Adding a SOP does not
grant new repair permissions or schedule an investigation.

## Add a procedure

1. Create `<name>/SKILL.md` in this directory.
2. Add YAML front matter with a unique `name` and a `description` of the
   symptom it handles. Write the checks, required evidence, allowed actions
   and human escalation conditions in the Markdown body.
3. If this introduces a new alert, register it in `ALERT_NAMES` in
   [the bridge](../bridge.py). This allowlist validates incoming alerts;
   it does not select SOPs. A new SOP for an existing alert needs no Python change.
4. Run provisioning to copy the files and restart Holmes with the new procedures.
