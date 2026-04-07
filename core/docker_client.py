"""Async wrapper around the synchronous Docker SDK for container discovery.

The Docker SDK (docker-py) is fully synchronous. All calls are dispatched to
a thread-pool executor so they never block the async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import docker
import docker.models.containers

from core.models import ContainerInfo

logger = logging.getLogger(__name__)

# Module-level client — created once on first use to reuse the socket connection pool.
_client: docker.DockerClient | None = None


def _get_client() -> docker.DockerClient:
    """Return the module-level Docker client, creating it lazily on first call."""
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _list_containers_sync() -> list[docker.models.containers.Container]:
    """Synchronous call to list running containers — must run in executor."""
    return _get_client().containers.list()


def _extract_ports(ports: dict[str, Any]) -> list[str]:
    """Return sorted port specs exposed by the container (e.g. ["443/tcp", "8080/tcp"]).

    Includes all declared ports regardless of host publishing — Caddy sits on the
    same Docker network (proxy_net) and can reach any container port by name,
    so unpublished ports are valid proxy targets.

    `ports` uses Any because docker-py ships no typed stubs for this dict.
    """
    return sorted(ports.keys())


def _container_to_info(container: docker.models.containers.Container) -> ContainerInfo:
    """Map a Docker SDK Container object to a ContainerInfo model."""
    # Docker sometimes prefixes container names with "/" — strip it for display.
    name = container.name.lstrip("/")
    image = container.image.tags[0] if container.image.tags else container.image.short_id
    return ContainerInfo(
        name=name,
        id=container.short_id,
        image=image,
        ports=_extract_ports(container.ports),
    )


async def list_running_containers() -> list[ContainerInfo]:
    """Return all currently running Docker containers with their exposed ports.

    Wraps the synchronous Docker SDK in a thread-pool executor to avoid
    blocking the async event loop. Returns an empty list if Docker is
    unreachable — the UI degrades gracefully without crashing.
    """
    loop = asyncio.get_running_loop()
    try:
        containers = await loop.run_in_executor(None, _list_containers_sync)
    except Exception as exc:
        logger.warning(f"Docker unavailable, container list empty: {exc}")
        return []
    return [_container_to_info(c) for c in containers]
