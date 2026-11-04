# HolmesGPT provisioning

HolmesGPT investigates Alertmanager alerts from the Linux demo VM. It queries
the local signals and calls a model server through its OpenAI-compatible API.
Reports and executor messages are in English.

## Start the demo

Select the demo inventory as described in the [inventory guide](../../docs/recipes.md).
Set the endpoint and model under `machines.holmes.environment` in `machines.yml`:

```yaml
environment:
  LLM_BASE_URL: http://your-lan-pc:1234/v1
  LLM_MODEL: google/gemma-4-12b-qat
```

LM Studio and Ollama both offer this API. With Ollama, for example, use
`http://your-lan-pc:11434/v1` and the installed model name, including its tag.
The model must support tool calling; changing the endpoint does not guarantee
the same diagnosis quality. Optional `LLM_EXTRA_BODY` is a JSON object for
model-specific request settings. Omit it when the server does not need them.

Enable the model server's LAN access and allow its port in the PC firewall.
The VM must be able to reach it. Labstack saves the variables when it creates
the VM; changing them requires `task destroy`, then `task up`.
The Holmes service loads `/etc/labstack/environment` through systemd and generates
its model configuration at startup. The address is not hardcoded in the scripts.

On macOS, allow the application used to start Labstack, such as iTerm or an IDE,
in System Settings > Privacy & Security > Local Network. This permission belongs
to the launching application: allowing iTerm or Python does not also allow an
IDE or an automation app. QEMU may otherwise reach the Internet but fail to reach
the LAN, even when `curl` on the Mac succeeds.

To restart an existing VM from an allowed application, without provisioning again:

```sh
task down
task up:no-provision
```

Open these local ntfy topics:

- [Raw alerts and resolutions](http://127.0.0.1:18081/alerts).
- [Holmes diagnoses, actions and escalations](http://127.0.0.1:18081/holmes).

## Diagnostics and SOPs

Each SOP has its own `<name>/SKILL.md` file under
[sops](assets/sops/README.md), with the same layout as the K3s procedures.

- HolmesGPT connected to the LAN model endpoint.
- Tools to inspect the demo's metrics, logs, traces and Linux service state.
- Separate [HTTP](assets/sops/http-service/SKILL.md),
  [file permission](assets/sops/file-permissions/SKILL.md),
  [CPU](assets/sops/cpu-pressure/SKILL.md),
  [memory](assets/sops/memory-pressure/SKILL.md),
  [disk space](assets/sops/disk-space/SKILL.md),
  [missing mount](assets/sops/missing-mount/SKILL.md) and
  [monitoring](assets/sops/monitoring/SKILL.md) procedures.

Connect with `task connect -- holmes` from the Mac, then inject a fault:

```sh
task fault:service
```

SOPs are always available through Holmes's native `custom_skill_paths` setting.
The model chooses a relevant procedure from its name and description in the
native skill catalogue, then loads it with `fetch_skill`. The bridge does not
map alert names to SOPs. There is no demo mode to select. Each action is checked
separately by the executor.

Holmes can load its SOP, inspect Linux state, logs and
metrics, and call the guarded start tool. The executor verifies an HTTP 200
response with the expected content before reporting recovery.

For a traced file access failure, run inside the VM:

```sh
task fault:permissions
```

Holmes can correlate the API HTTP 503 with Loki's `PermissionError` and a
failed file access span in Jaeger, then request permission recovery to `0644`.
Use the [observability guide](../observability/README.md) to follow the same
request in Grafana. Manual recovery is `task restore:permissions`.

For an escalation example, run `task fault:file` to move the message aside.
FastAPI reports a missing file; the executor cannot recreate it. Holmes must
request human intervention. Use `task restore:file` afterwards. Memory,
disk saturation, missing mounts and monitoring failures have no automatic
repair. The [fault tasks](../observability/README.md#inject-and-recover-demo-faults)
cover these scenarios, with individual recovery commands and a global `restore`.
Inside the VM, use `task fault:service`, `task restore` and `task` to list all tasks.

For CPU recovery, run `task fault:cpu`. Holmes can stop only the
known `labstack-demo-cpu.service` transient job, including its child processes.
The executor checks the command, current job identity, a matching firing alert,
host CPU above 85% and this job using at least 50% of total VM CPU. It then verifies
that the job stopped, CPU fell below 85% and the alert cleared. A replaced,
expired or unrelated process is refused. Memory stress remains a human recovery:
`task restore:memory`; no tool grants permission to stop it.

From the Mac, check this path against the real model with:

```sh
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/holmes-cpu.sh
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/holmes-memory.sh
```

The CPU check requires a verified stop in the executor journal; a model report
claiming success does not count. The RAM check requires the memory SOP, live
status, metrics and logs, a delivered report and no mutation. It checks memory
pressure, not an injected OOM kill. Both use the current model settings.

## Recovery and notifications

- An Alertmanager investigation trigger.
- A small set of repair operations with fixed demo targets.
- Recovery verification, an action journal and notifications when a repair is
  unavailable or fails.
- Investigation results and repair status in a separate ntfy `holmes` topic;
  Alertmanager continues sending alerts and resolutions to `alerts`.

The executor accepts no paths, service names or shell commands from the model.
It rechecks the firing alert and live target state, allows one attempt per
incident and enforces a one-minute global cooldown. A refused or unsuccessful
repair requires an operator. The model's prose and the executor's actual result
are shown separately in the notification.

The queue and action journal survive bridge restarts. An interrupted investigation
is escalated and its action is never repeated automatically. Notification delivery
can be retried. If an investigation fails or times out, the bridge stops accepting
new tool work to avoid overlapping model requests. Investigate the failure, then
restart inside the VM with `sudo systemctl restart labstack-holmes-bridge`
after confirming no model request is still running.

Wait for the current investigation before injecting another fault. Model latency and tool
round trips are added to the short alert delays used for the demo.
If the model stops at a plan while a repair is still possible, the bridge allows
one continuation with the same conversation and evidence. It never repeats an
action attempt. A second incomplete answer is escalated to the presenter.

## Check the setup

Run the model checks from the repository on the Mac:

```sh
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/holmes.sh
task connect -- holmes
```

Inspect the services inside the VM:

```sh
sudo journalctl -u labstack-holmes-bridge -n 40 --no-pager
cd /opt/labstack-holmes
sudo docker compose logs --tail 60 holmes
sudo ls /var/lib/labstack-holmes
```

The runtime test injects reversible faults against the real model and restores
the demo targets when it exits. Keep the Mac awake while the checks run.
Logs and traces are limited to the incident window, with a five-minute maximum,
and the model selects its SOP from the native catalogue.
Full Holmes responses, including tool results,
are kept in `/var/lib/labstack-holmes/<incident-id>.json`; incident and action
state are in `incidents.db`. These files stay inside the disposable VM.

Holmes runs in an unprivileged container without a Docker socket or host shell.
Its fixed tools call a loopback-only executor using a private generated token.
The executor runs as root to read the Linux journal and perform the three fixed
repairs. The demo has local anonymous dashboards and notifications; its web ports
are forwarded only on the Mac's loopback interface.

See the [inventory guide](../../docs/recipes.md) to select the lab.
