# container_weekly_digest

Sends a weekly fleet summary to all allowed users: top containers by CPU and RAM,
plus any containers that have accumulated restarts.

Scheduled via cron. Also available as an on-demand button at any time.

## Config

```yaml
container_weekly_digest:
  schedule: "0 8 * * 1"   # Monday 08:00
  top_n: 5                 # containers to show in each ranking
```

## Note

Uses `docker stats --no-stream`. If the socket-proxy blocks the `/stats` API endpoint,
add it to the allowlist in the socket-proxy config.

## Button

`📊 Fleet Digest` — on-demand snapshot of current CPU/RAM + restart counts.
