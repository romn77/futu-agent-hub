"""Local JSON cache protocol; public ingestion does not execute source content."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from .paths import public_root
from .public import PublicCache
from .store import CacheStore
from .types import CacheError, unique_json


def read_input(path, *, limit=16 * 1024 * 1024, private=False):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or path.resolve() != path:
        raise CacheError("UNSAFE_PATH")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or info.st_uid != os.getuid() or (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise CacheError("UNSAFE_PATH")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise CacheError("TOO_LARGE")
    return unique_json(content)


def parser():
    root = argparse.ArgumentParser(description="Private local research cache; no network or account reads")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("lookup", "reserve", "publish", "invalidate", "guard", "status", "prune", "release", "renew", "rate-limit"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
        command.add_argument("--scope", choices=("public", "private"), default="public")
        command.add_argument("--request")
        command.add_argument("--manifest")
        command.add_argument("--payload")
        command.add_argument("--lease")
        command.add_argument("--off", action="store_true")
    return root


def execute(args):
    if args.off:
        return {"status": "BYPASS", "reason": "CACHE_DISABLED"}
    request = read_input(args.request) if args.request else {}
    root = public_root()
    if args.scope == "private":
        from ..settings import Settings
        root = Settings.from_env().data_home / "cache/v1"
    store = CacheStore(root, scope=args.scope)
    if args.command in {"status", "prune"}:
        return getattr(store, args.command)()
    if args.scope != "public":
        raise CacheError("ACCOUNT_UNVERIFIED")
    public = PublicCache(store)
    if args.command == "lookup":
        if not set(request) <= {"query", "cutoff", "historical", "force"}:
            raise CacheError("INVALID_INPUT")
        return public.lookup(**request)
    if args.command == "reserve":
        from .public import validate_query
        validate_query(request["query"])
        return store.reserve(request["query"], seconds=request.get("seconds", 60))
    if args.command in {"release", "renew"}:
        lease = read_input(args.lease, private=True)
        result = getattr(store, args.command)(lease)
        return result or {"status": "RELEASED"}
    if args.command == "rate-limit":
        if request != {"source": "SEC"}:
            raise CacheError("INVALID_INPUT")
        store.rate_limit("SEC", interval=0.5)
        return {"status": "READY"}
    if args.command in {"invalidate", "guard"}:
        if set(request) != {"tag"} or not isinstance(request["tag"], str):
            raise CacheError("INVALID_INPUT")
        return getattr(store, args.command)(request["tag"])
    manifest = read_input(args.manifest)
    payload = read_input(args.payload, limit=64 * 1024 * 1024)
    lease = read_input(args.lease, private=True) if args.lease else None
    if lease is None:
        raise CacheError("LEASE_REQUIRED")
    kind = manifest.pop("kind", "public_evidence")
    query = manifest.pop("query")
    if kind == "public_summary":
        return public.publish_summary(query, payload, lease=lease, **manifest)
    if kind != "public_evidence":
        raise CacheError("INVALID_INPUT")
    return public.publish(query, payload, lease=lease, **manifest)


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (CacheError, OSError, ValueError, TypeError, KeyError) as exc:
        code = exc.code if isinstance(exc, CacheError) else "INVALID_INPUT"
        print(json.dumps({"status": "ERROR", "reason": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
