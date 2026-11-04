---
name: demo-disk-space
description: Investigate DemoDiskSpaceLow for the affected filesystem and request human recovery without deleting data.
---
1. Call read_demo_metrics, read_demo_status and read_demo_logs once each.
2. Match the alert's mountpoint to filesystem metrics and the df output.
   Report available space and the available / size ratio as a percentage.
   Values belong to that filesystem; never attribute root usage to the demo
   volume when /srv/demo is not mounted. Missing samples are unknown evidence.
3. For /srv/demo, confirm the dedicated mount is present in the mount and
   systemd views. An absent mount is a different fault, not a full volume.
   If space has already recovered, report the current measurement.
4. Correlate any recent write or I/O error with the affected filesystem.
   A usage ratio does not identify which file caused it. These tools do not
   list file sizes or inode usage; ask the operator for those checks if needed.
5. Request human inspection of that filesystem. If the operator confirms the
   demo filler file, propose task restore:disk, which removes only that file.
   Do not suggest deleting application data or filling another filesystem.

No automatic repair is authorized. Never delete or truncate files, change
permissions, remount storage, resize filesystems or reboot. Report the measured
space, remaining uncertainty and that human intervention is required.
