"""Local evidence and analysis caching; legacy commands remain opt-in callers."""

from .store import CacheStore
from .types import CacheError

__all__ = ["CacheError", "CacheStore"]
