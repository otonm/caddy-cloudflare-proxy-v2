# Plan 04 — Docker Client

## Goal

Implement `core/docker_client.py`: a thin async wrapper around the Docker SDK that
lists running containers with their names, images, and exposed ports. The result feeds
the "target" dropdown in the UI when the user selects `target_type = DOCKER`.

---

## Dependencies on Previous Plans

- Plan 03: uses `ContainerInfo` from `core/models.py`.
- Plan 01: `docker>=7.0` must be in `pyproject.toml`.

---

## Why This Needs Care

The Docker SDK (`docker-py`) is **synchronous**. Calling it directly in an async
function blocks the event loop, freezing the entire UI. All Docker SDK calls must be
wrapped in `asyncio.get_event_loop().run_in_executor(None, ...)` to run them in the
default thread pool executor.

---

## File: `core/docker_client.py`

### Public API

```python
async def list_running_containers() -> list[ContainerInfo]:
    """Return all currently running Docker containers with their exposed ports.

    Uses the Docker socket mounted at /var/run/docker.sock (read-only).
    Returns an empty list if Docker is unreachable rather than raising,
    since Docker connectivity is optional — the UI should degrade gracefully.
    """
```

### Implementation Notes

**Client lifecycle**: Create a `docker.from_env()` client once (module-level or lazily
on first call), not on every request. The client holds a connection pool to the Docker
socket. Close it on app shutdown if possible.

**Pattern for async wrapping**:
```python
import asyncio
import docker

_client: docker.DockerClient | None = None

def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client

def _list_containers_sync() -> list[docker.models.containers.Container]:
    return _get_client().containers.list()

async def list_running_containers() -> list[ContainerInfo]:
    loop = asyncio.get_event_loop()
    try:
        containers = await loop.run_in_executor(None, _list_containers_sync)
    except Exception as exc:
        logger.warning("Docker unavailable: %s", exc)
        return []
    return [_container_to_info(c) for c in containers]
```

**`_container_to_info` mapping**:
- `name`: `container.name` (strip leading `/` which Docker sometimes adds)
- `id`: `container.short_id` (12-char prefix)
- `image`: `container.image.tags[0]` if tags else `container.image.short_id`
- `ports`: extract from `container.ports` — the dict maps port specs to host bindings.
  Collect the keys (e.g., `"8080/tcp"`) to show what ports are exposed inside
  the container. Only include ports that are exposed (key exists in the dict),
  not necessarily published to the host.

**Port extraction**:
```python
def _extract_ports(ports: dict) -> list[str]:
    # ports is like {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}], "443/tcp": None}
    # We want the keys where the value is not None (actually exposed)
    # But for proxy purposes, we want all declared container ports, even unpublished ones,
    # since Caddy is on the same Docker network and can reach any container port directly.
    return sorted(ports.keys())  # e.g. ["443/tcp", "8080/tcp"]
```

> **Why include unpublished ports**: Caddy and the target containers are on the same
> Docker network (`proxy_net`). Caddy can reach any container port by container name,
> regardless of whether it's published to the host. So all exposed (EXPOSE) ports are
> valid targets.

**Target value format**: When the user selects a container as a target, the
`target_value` stored in `ProxyEntry` should be `"container_name:port"`, e.g.
`"my-app:8080"`. The UI should prompt the user to select both the container and
the specific port.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Docker socket not found | Log WARNING, return `[]` |
| Permission denied | Log WARNING with explanation, return `[]` |
| Container list call fails | Log WARNING, return `[]` |

Never raise from `list_running_containers()` — a Docker failure should not crash the UI.

---

## Test Considerations

- The Docker client is hard to unit-test without a real socket. Skip unit tests for now.
- Integration test: run `docker compose up` and verify the app can list the `caddy`
  container at minimum.
- Mock the `_list_containers_sync` function at the executor boundary for unit tests
  if needed later.

---

## Verification Steps

1. `uv run python -c "import asyncio; from core.docker_client import list_running_containers; print(asyncio.run(list_running_containers()))"` — must return a list (possibly empty if Docker socket isn't available locally).
2. `uv run ruff check core/docker_client.py --fix` — must pass clean.

---

## Open Questions

- **Container name extraction**: Some containers have names like `/my-app` (with leading
  slash). Should we strip the slash when displaying in the UI? Yes — always strip
  leading `/`.
- **Port selection UX**: When a container has multiple ports (e.g., `8080/tcp` and
  `9090/tcp`), the UI needs to let the user pick one. The `ContainerInfo.ports` list
  handles this. The form (Plan 10) will render a second dropdown for port selection.
