---
name: demo-file-permissions
description: Diagnose DemoApiUnavailable using Linux state, Loki errors and Jaeger traces, then repair only the demo file permission fault.
---
1. Call read_demo_status, read_demo_metrics, read_demo_logs and read_demo_traces.
2. Correlate FastAPI HTTP 503 with PermissionError in Loki and a failed
   read demo message span in Jaeger. Include one matching trace ID in the report.
3. Verify that demo-fastapi is active, /srv/demo is mounted and message.txt is
   a regular file with mode 000. A missing mount, missing file, full disk or
   stopped FastAPI is a different cause: request human intervention.
4. When those facts agree, call restore_demo_file_permissions once. Its executor
   restores only /srv/demo/message.txt to 0644 and verifies HTTP 200 and content.
5. Report evidence, exact action and the executor's verification result.

Never create or replace a missing file, remount storage, delete data, change
ownership or grant broader permissions. Do not retry refused or failed repairs.
