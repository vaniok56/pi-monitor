# cert_expiry_watcher

Checks TLS certificate expiry for configured domains using Python's `ssl` module.
Alerts when any cert is within `warn_days` of expiry.

## Config

```yaml
cert_expiry_watcher:
  domains:
    - example.com
    - subdomain.example.com:443  # optional custom port
  warn_days: 14
  schedule: "0 9 * * *"
```

## Button

`🔒 Cert Expiry` — table of all domains with days remaining and expiry date.
