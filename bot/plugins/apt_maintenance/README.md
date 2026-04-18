# apt_maintenance plugin

## Purpose
Open instantly with a small action menu, then run host APT maintenance from Telegram using separate actions:

1. Update flow: apt-get update + apt-get upgrade -y
2. Cleanup flow: apt-get autoremove -y + apt-get clean

## Configuration (plugins.yml)
```yaml
enabled:
  apt_maintenance:
    max_listed_updates: 20      # how many package names to show in the report
    helper_image: "alpine:3.20" # helper image used for nsenter host execution
```

## Actions and buttons
- Button: 📦 APT maintenance -> p.apt_maintenance:menu
- Entry screen is fast (no apt commands are run at open).
- Entry actions:
  - ⬆️ Update -> runs update preview first, then confirmation, then update+upgrade
  - 🧹 Cleanup -> confirmation, then autoremove+clean
- After execution, plugin returns to the menu with last-run summary.

## Docker-related safeguard
If update preview contains docker-related packages (for example: docker, containerd, runc, compose, moby), an extra confirmation screen is shown before upgrade starts.

## Execution model
Host commands run through a short-lived privileged helper container using nsenter into host namespaces, so APT runs on the host rather than inside the bot container.

## Output summary
After execution, the plugin summarizes key output details such as:
- upgraded/newly installed/to remove/not upgraded counts
- how many packages were autoremove-removed (cleanup)
- space freed when reported
- error tail on failure

## Notes
- Designed for Debian/Ubuntu-family hosts.
- If apt-get is not available on the host, plugin reports unsupported and does not modify anything.
