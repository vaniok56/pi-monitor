"""Platform and capability detection — runs once at startup."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostInfo:
    host_class: str   # rpi | debian_amd64 | debian_arm64 | mac_intel | mac_apple_silicon | linux_other
    host_label: str
    capabilities: dict


def _detect_host_class() -> str:
    try:
        model = Path("/sys/firmware/devicetree/base/model").read_text().rstrip("\x00").lower()
        if "raspberry" in model:
            return "rpi"
    except Exception:
        pass

    machine = platform.machine().lower()
    system = platform.system().lower()

    if system == "darwin":
        return "mac_apple_silicon" if machine in ("arm64", "aarch64") else "mac_intel"

    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "debian_amd64"
        if machine in ("aarch64", "arm64"):
            return "debian_arm64"

    return "linux_other"


def _probe_capabilities() -> dict:
    return {
        "vcgencmd": shutil.which("vcgencmd") is not None,
        "smartctl": shutil.which("smartctl") is not None,
        "apt": shutil.which("apt") is not None,
        "systemctl": shutil.which("systemctl") is not None,
    }


def _detect_docker_daemon_hostname() -> str:
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0:
            name = proc.stdout.strip()
            if name:
                return name
    except Exception:
        pass
    return ""


def detect() -> HostInfo:
    host_class = _detect_host_class()
    host_label = (
        os.environ.get("HOST_LABEL", "").strip()
        or _detect_docker_daemon_hostname()
        or socket.gethostname()
    )
    capabilities = _probe_capabilities()
    return HostInfo(
        host_class=host_class,
        host_label=host_label,
        capabilities=capabilities,
    )
