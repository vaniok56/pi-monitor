"""
Compose-file dependency parser.

Reads the compose file for a container's project (via
com.docker.compose.project.config_files label), parses the depends_on
graph, and returns sibling health for crash-alert enrichment.

Results are cached by (config_files_path, mtime) so repeated calls
within the same second are free.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import docker
import yaml

logger = logging.getLogger(__name__)

# Cache: (config_path, mtime) → {service_name: [dep_names]}
_cache: dict[tuple[str, float], dict[str, list[str]]] = {}


def _load_deps_graph(config_path: str) -> dict[str, list[str]]:
    """Parse a compose file and return {service: [depends_on...]}."""
    try:
        mtime = os.path.getmtime(config_path)
    except OSError:
        return {}
    key = (config_path, mtime)
    if key in _cache:
        return _cache[key]

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.debug("deps: cannot parse %s: %s", config_path, exc)
        return {}

    graph: dict[str, list[str]] = {}
    for svc, cfg in (data.get("services") or {}).items():
        deps = cfg.get("depends_on") or []
        if isinstance(deps, dict):
            deps = list(deps.keys())
        graph[svc] = list(deps)

    _cache[key] = graph
    return graph


def _container_service(container) -> str:
    """Return the compose service name for a container."""
    return container.labels.get("com.docker.compose.service", container.name)


def dependencies_of(container) -> list[tuple[str, str]]:
    """
    Return [(sibling_name, health_emoji), ...] for containers that
    `container` directly depends on (via depends_on in the compose file).

    health_emoji: 🟢 running, 🔴 stopped, 🟡 restarting, ⚪ created/paused
    """
    config_files = container.labels.get("com.docker.compose.project.config_files", "")
    if not config_files:
        return []

    # config_files may be comma-separated; use the first one
    config_path = config_files.split(",")[0].strip()
    graph = _load_deps_graph(config_path)
    if not graph:
        return []

    service = _container_service(container)
    dep_services = graph.get(service, [])
    if not dep_services:
        return []

    # Resolve sibling containers
    project = container.labels.get("com.docker.compose.project", "")
    try:
        client = docker.from_env()
        all_containers = client.containers.list(all=True)
    except Exception:
        return []

    # Build service→container map for this project
    svc_map: dict[str, object] = {}
    for c in all_containers:
        if c.labels.get("com.docker.compose.project") == project:
            svc = c.labels.get("com.docker.compose.service", c.name)
            svc_map[svc] = c

    result: list[tuple[str, str]] = []
    for dep in dep_services:
        sibling = svc_map.get(dep)
        if sibling is None:
            result.append((dep, "❓"))
            continue
        status = sibling.status
        emoji = {
            "running": "🟢",
            "exited": "🔴",
            "dead": "🔴",
            "restarting": "🟡",
            "paused": "⏸",
            "created": "⚪",
        }.get(status, "❓")
        result.append((sibling.name, emoji))

    return result
