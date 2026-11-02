# HolmesGPT provisioning

This directory will contain HolmesGPT's single-VM demo recipes, configuration,
SOPs and tools. The model will run in LM Studio on the presenter's LAN PC.

## Diagnostics

- HolmesGPT connected to the LAN model endpoint.
- Tools to inspect the demo's metrics, logs and Linux service state.
- Separate SOP files and a comparison with and without SOPs.

## Recovery and notifications

- An Alertmanager investigation trigger.
- A small set of repair operations with fixed demo targets.
- Recovery verification, an action journal and notifications when a repair is
  unavailable or fails.
- Investigation results and repair status in a separate ntfy `holmes` topic;
  Alertmanager continues sending alerts and resolutions to `alerts`.

Provisioning scripts and repair tools are not implemented yet.
Future scripts must be added explicitly to the active `machines.yml`.

See the [inventory guide](../../docs/recipes.md) to select the lab.
