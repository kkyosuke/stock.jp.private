"""Resolve explicitly configured, local-only market data without public fallback."""
from __future__ import annotations
from stock_jp.workspace import record_path

import json
from pathlib import Path
from typing import Any


class MarketDataStorageError(ValueError):
    """The configured private archive cannot safely be used."""


def resolve_archive_root(root: Path, config: dict[str, Any] | None = None) -> Path:
    if config is None:
        try:
            config = json.loads((root / 'config/sources.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise MarketDataStorageError('private market-data configuration is unavailable') from error
    price = config.get("price_source", {})
    if not isinstance(price, dict) or price.get("storage") != "private_local":
        raise MarketDataStorageError("price_source.storage must explicitly be private_local")
    configured = price.get("archive_root")
    if not isinstance(configured, str) or not configured.strip():
        raise MarketDataStorageError("private archive_root is required")
    relative = Path(configured)
    if relative.is_absolute() or ".." in relative.parts:
        raise MarketDataStorageError("archive_root must be a private workspace-relative path")
    # operations/private may be the approved workspace symlink. Descendant
    # symlinks may not escape this boundary into public or unrelated storage.
    root = root.resolve()
    allowed = root / '.work/market-data'
    if allowed.resolve() != allowed:
        raise MarketDataStorageError('market-data storage must not escape through a symlink')
    archive = (root / relative).resolve()
    if not archive.is_relative_to(allowed) or archive == allowed:
        raise MarketDataStorageError("archive_root must stay inside private market-data storage")
    if not archive.is_dir():
        raise MarketDataStorageError("private market-data archive is missing; no public fallback")
    return archive
