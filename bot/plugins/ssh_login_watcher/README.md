# ssh_login_watcher

Polls `/var/log/auth.log` (via `nsenter`) for failed SSH login attempts.
Fires an alert when any single IP exceeds `threshold` failures in `window_minutes`.

## Config

```yaml
ssh_login_watcher:
  auth_log: /var/log/auth.log
  threshold: 10           # alert when an IP hits this many failures
  window_minutes: 5       # sliding window
  interval_seconds: 120   # how often to poll
```

## Button

`🔐 SSH Watcher` — top offending IPs with attempt counts and targeted usernames.
