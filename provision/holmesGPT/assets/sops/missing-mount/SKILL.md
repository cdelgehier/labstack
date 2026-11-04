---
name: demo-missing-mount
description: Investigate DemoExpectedMountMissing by checking the expected demo mount and systemd state, without recreating data or remounting.
---
1. Call read_demo_metrics, read_demo_status and read_demo_logs once each.
2. Confirm /srv/demo is declared in labstack_expected_mount. Correlate the
   alert with findmnt and srv-demo.mount ActiveState, SubState and Result.
   A missing exporter sample alone does not prove that the mount disappeared.
3. Distinguish an inactive mount from an active mount whose telemetry is
   missing or unreadable. Use recent mount or kernel I/O errors only when
   they are present. Do not invent a failed disk or an unmount command.
4. df /srv/demo can show the root filesystem when the mount is absent.
   Do not interpret that as demo-volume capacity. A missing message and API
   503 can be consequences of the absent volume, not a permission problem.
5. Request operator inspection with systemctl status srv-demo.mount and the
   recent unit journal. Once the backing image is confirmed intact, the
   operator can use task restore:mount and check findmnt and API health again.

No automatic repair is authorized. Never mount, format, create or replace
/srv/demo/message.txt on the root filesystem, change permissions or reboot.
State what proves the mount state, what remains unknown and that no repair
was executed. If the mount is already back, report the current evidence.
