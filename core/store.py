"""Async JSON persistence store for proxy configuration.

All reads and writes go through this module. Writes are atomic (write to .tmp,
then os.replace) so a crash mid-write never leaves a corrupt config file.
Domain uniqueness is enforced here — the model layer is a pure value object.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import aiofiles

import core.config as config
from core.models import ProxyConfig, ProxyEntry

logger = logging.getLogger(__name__)


class DomainExistsError(ValueError):
    """Raised when attempting to add an entry for a domain that already exists.

    Carries the existing entry's ID so the caller can offer "edit existing" navigation.
    """

    def __init__(self, domain: str, existing_id: uuid.UUID) -> None:
        super().__init__(f"Domain {domain!r} already has a proxy entry")
        self.domain = domain
        self.existing_id = existing_id


async def load_config() -> ProxyConfig:
    """Load the full proxy config from disk.

    Creates and persists an empty ProxyConfig if the file does not yet exist
    (first run in a fresh container).
    """
    try:
        async with aiofiles.open(config.CONFIG_FILE, encoding="utf-8") as f:
            content = await f.read()
        cfg = ProxyConfig.model_validate_json(content)
        logger.debug(f"Loaded config: {len(cfg.entries)} entries")
        return cfg
    except FileNotFoundError:
        logger.info(f"Config file not found at {config.CONFIG_FILE} — creating empty config")
        cfg = ProxyConfig()
        await save_config(cfg)
        return cfg


async def save_config(cfg: ProxyConfig) -> None:
    """Atomically write the full proxy config to disk.

    Writes to a .tmp sibling first, then renames via os.replace(), which is
    atomic on POSIX. Both the mkdir and rename are offloaded to a thread so
    the event loop is not blocked.
    """
    tmp_path = config.CONFIG_FILE.with_suffix(".tmp")
    # Ensure parent directory exists (first run before volume is mounted)
    await asyncio.to_thread(config.CONFIG_FILE.parent.mkdir, parents=True, exist_ok=True)
    async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
        await f.write(cfg.model_dump_json(indent=2))
    await asyncio.to_thread(os.replace, tmp_path, config.CONFIG_FILE)
    logger.debug(f"Config saved: {len(cfg.entries)} entries")


async def get_entry(entry_id: uuid.UUID) -> ProxyEntry | None:
    """Return a single entry by ID, or None if not found."""
    cfg = await load_config()
    for entry in cfg.entries:
        if entry.id == entry_id:
            return entry
    return None


async def add_entry(entry: ProxyEntry) -> ProxyEntry:
    """Append a new entry and persist the config.

    Raises DomainExistsError (carrying the existing entry's ID) if the domain
    already has a proxy entry. The caller can use the ID to navigate to the
    existing entry for editing.
    """
    cfg = await load_config()
    for existing in cfg.entries:
        if existing.domain == entry.domain:
            raise DomainExistsError(entry.domain, existing.id)
    cfg.entries.append(entry)
    await save_config(cfg)
    logger.info(f"Added entry: {entry.domain} → {entry.target_value}")
    return entry


async def update_entry(entry: ProxyEntry) -> ProxyEntry:
    """Replace an existing entry in-place (preserves list order) and persist.

    Raises KeyError if no entry with the given ID exists.
    Domain is considered read-only on edit (enforced by the UI), so no
    domain-uniqueness re-check is needed here.
    """
    cfg = await load_config()
    for i, existing in enumerate(cfg.entries):
        if existing.id == entry.id:
            cfg.entries[i] = entry
            await save_config(cfg)
            logger.info(f"Updated entry: {entry.domain} → {entry.target_value}")
            return entry
    raise KeyError(f"No entry with id={entry.id}")


async def delete_entry(entry_id: uuid.UUID) -> ProxyEntry:
    """Remove an entry by ID and persist. Returns the removed entry.

    Raises KeyError if no entry with the given ID exists.
    """
    cfg = await load_config()
    for i, existing in enumerate(cfg.entries):
        if existing.id == entry_id:
            removed = cfg.entries.pop(i)
            await save_config(cfg)
            logger.info(f"Deleted entry: {removed.domain}")
            return removed
    raise KeyError(f"No entry with id={entry_id}")


async def list_entries() -> list[ProxyEntry]:
    """Return all entries ordered by created_at ascending."""
    cfg = await load_config()
    return sorted(cfg.entries, key=lambda e: e.created_at)
