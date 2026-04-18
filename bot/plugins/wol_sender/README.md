# wol_sender plugin

## Purpose
Sends Wake-on-LAN (WoL) magic packets to configured machines from Telegram. Useful for powering on a NAS, desktop, or any WoL-capable device remotely.

## Configuration (`plugins.yml`)
```yaml
enabled:
  wol_sender:
    targets:
      - name: "My NAS"
        mac: "AA:BB:CC:DD:EE:FF"
      - name: "Desktop PC"
        mac: "11:22:33:44:55:66"
```

Each target requires:
- `name` — display name shown in the Telegram button
- `mac` — MAC address of the target machine (any standard format)

## Actions and buttons
- Button: `💡 Wake-on-LAN` → `p.wol_sender:menu` → per-target buttons `💡 Wake <name>`

## What it executes
- Calls `wakeonlan.send_magic_packet(<mac>)` from the `wakeonlan` Python library.
- Magic packet is broadcast on the local network from the host running the bot.

## Output and failure behavior
- Success: `✅ Magic packet sent to <name> (<mac>).`
- Failure: `❌ WoL failed: <error>`
- If no targets are configured, plugin logs a warning and does not register.
- Exit navigation returns to the Plugins list (`plugins_menu`).

## Requirements
- `wakeonlan` Python package (included in bot dependencies).
- Target machine must have Wake-on-LAN enabled in BIOS/UEFI and be connected via Ethernet.
- Bot host must be on the same LAN segment as the target (WoL broadcasts do not cross routers by default).
