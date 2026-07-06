# mergerfs_health

Reports per-mount disk usage on the **host** (via `nsenter` in a privileged helper container).
Covers any mount point — mergerfs pools, individual drives, backup SSDs.

## Config

```yaml
mergerfs_health:
  mounts:
    - /mnt/disk1
    - /mnt/disk2
    - /mnt/immich-storage
    - /mnt/backup-ssd
  alert_pct: 85        # alert when usage >= this %
  schedule: "0 */6 * * *"
```

## Button

`🗂 Disk Health` — visual bar chart per mount with used/free/total.
