# uptime_kuma_summary

Shows Uptime Kuma monitor statuses using the public status-page API.
No authentication required — monitors must be added to the named status page.

## Config

```yaml
uptime_kuma_summary:
  url: http://192.168.1.10:3001  # or container name if on same network
  slug: default                   # status page slug
  timeout: 10
```

## Network note

If the bot and Uptime Kuma are on different Docker networks, use the host IP
(`192.168.1.10:3001`) or connect the networks. The container name `uptime-kuma`
only resolves if both containers share a network.

## Button

`📡 Uptime Kuma` — live status of all monitors on the page with ping and 24h uptime.
