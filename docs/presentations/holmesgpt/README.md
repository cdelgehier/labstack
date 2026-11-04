# HolmesGPT talk

A French Slidev presentation and an English B2 version with Neversink about HolmesGPT investigations,
observability, SOPs and guarded repairs on a Linux demo VM.
Speaker notes include source paths, known limits
and commands for the live demo. Identifiers, shell commands and configuration
keys keep their original names. The SOP example is translated into French.

## Present

Use Node.js 22.12 or newer. From this directory:

```sh
npm ci
npm run dev
```

Open <http://localhost:3030>. Press `p` for the presenter view and its notes,
or `o` for the slide overview. Space advances the next reveal or slide.

The English version is in `slides_en.md`, including diagrams and speaker notes.
Run `npm run dev:en` to present it at <http://localhost:3031>.

```sh
npm run build
npm run build:en
npm run export
npm run export:en
```

Builds are written to `dist/` and `dist_en/`. PDF exports are `holmesgpt.pdf`
(French) and `holmesgpt_en.pdf` (English), with one page per slide.
Chromium is required for export; if the npm install skipped its download,
run `npx playwright install chromium` first.
Fonts come from local npm packages. Slides do not fetch live infrastructure
data. External source links and the live demo need their usual network access.

## Reading paths

The main presentation introduces HolmesGPT and the investigation workflow,
then follows one incident from an HTTP alert to evidence, a guarded repair and its report.
It is organized into six parts:

1. Why HolmesGPT: the operational problem, the project, investigation triggers,
   the numbered alert workflow and the different roles of the model and tool controls.
2. Investigation evidence: illustrated metrics, logs and traces, with examples
   and the complementary role of Linux or Kubernetes state.
3. The demo environment: the Linux VM and signal collection.
4. An investigation: a file permission fault, diagnosis, repair and verification.
5. Controlling the agent: SOPs, permissions and escalation.
6. Investigation failures and resources: observed failures, investigation
   budgets, context and memory limits.

The live demonstration follows the memory slide, with RAM pressure, CPU pressure
and the missing-file fault, then the comparison of the RAM and CPU investigations.

The final [notification example](http://localhost:3030/exemple-rapport) shows
a real ntfy report for a resolved CPU incident. SOPs, budgets, memory experiments
and optimization guidance stay in the main presentation.
Speaker notes preserve sources, qualifications and demo commands.

In the demo, one FastAPI/Uvicorn service (`demo-fastapi`) serves both the home
page and `/api/check`, without a reverse proxy. The API reads
the demo file. FastAPI instrumentation creates the HTTP span automatically,
and the application adds a child span around the file read. Logs carry the
same trace ID.

## Source material

The [CPU notification](evidence/ntfy-cpu-resolved-2026-11-04.json) was read
from the demo's ntfy topic on 4 November 2026. The report example shows a
translated, shortened excerpt, including the independent executor result.
The archive preserves the full original English message.

The archived [notification excerpt](evidence/ntfy-refusal-2026-11-02.txt) was
copied from ntfy by Cédric. It records the model's report of a refused call,
without an independent executor result. It is not shown in the presentation.

The investigation-step slide recalls the replay with more steps. The memory
slide and its speaker notes cover the separate memory-limit test on the same
`kubectl` read. These are historical observations, not new runs of the current
demo VM.

## Before the live demo

Start the lab with `task up` from the repository root. Check that the configured
model is loaded, supports tool calling and is reachable from the VM.
Open the [observability guide](../../../provision/observability/README.md)
and the [Holmes guide](../../../provision/holmesGPT/README.md).

The [demo slide](http://localhost:3030/demo) has the live demo commands in its
speaker notes. Run memory pressure, CPU pressure and the missing-file fault
in that order. For the last fault, FastAPI stays active while `/api/check`
returns HTTP 503. Inject faults only in the Labstack VM. Wait for each investigation and the resolved alert
before starting the next one. Use the manual recovery commands when needed.
The alert delays are short for a demo; model inference adds its own delay.

## Sources and scope

- [HolmesGPT 0.42.0 documentation](https://holmesgpt.dev/0.42.0/).
- [CNCF project page](https://www.cncf.io/projects/holmesgpt/) for the project's
  Sandbox acceptance date.
- [Current operator documentation](https://holmesgpt.dev/operator/) for one-time,
  scheduled and deployment-triggered investigations. The operator is alpha and
  is not installed in the demo VM; check version support before using its resources.
- [Arik Alon's auto-remediation presentation](https://hosted-files.sched.co/kccnceu2026/9f/holmesgpt%20auto%20remediation.pdf)
  for the deployment regression and corrective pull request example.
- [Sathish Mohanram's alert automation presentation](https://prezi.com/page/tcg-k2ujr-yd/sre-alert-automation-with-holmesgpt-robusta)
  for orchestration, conditional exclusions in runbooks and model evaluation.
- Labstack: `machines.yml`, `provision/holmesGPT/` and `provision/observability/`.
- [Neversink](https://github.com/gureckis/slidev-theme-neversink).

Configuration extracts use hostnames in place of LAN addresses. They show
selected fields, not complete deployment files. The notification slide presents
documented integrations as options; the demo uses a custom HTTP relay to ntfy.
External performance claims are not presented as measurements of this lab.
