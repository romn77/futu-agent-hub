"""Resolve user-local roots without reading brokerage configuration."""

import os
from pathlib import Path
import sys

from .store import CacheStore
from .types import CacheError


def public_root():
    explicit = os.environ.get("RESEARCH_CACHE_HOME")
    if explicit:
        return Path(explicit).expanduser().absolute()
    base = (Path.home() / "Library/Caches" if sys.platform == "darwin"
            else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))))
    return base / "futu-agent-research" / "v1"


def stores(data_home, *, enabled=True, clock=None):
    options = {"enabled": enabled}
    if clock is not None:
        options["clock"] = clock
    public = CacheStore(public_root(), scope="public", **options)
    private = CacheStore(Path(data_home) / "cache/v1", scope="private", **options)
    if public.root.is_relative_to(private.root) or private.root.is_relative_to(public.root):
        raise CacheError("UNSAFE_PATH")
    peers = {"public": public, "private": private}
    public.peers = private.peers = peers
    return public, private
