"""Per-process SDK session with bounded, SEC-only streamed reads."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import importlib.metadata
import os
import tempfile
import threading
import time

from .evidence import ResearchError, require

_SESSION_LOCK = threading.Lock()


@dataclass
class Budget:
    max_bytes: int = 200 * 1024 * 1024
    seconds: float = 600
    used_bytes: int = 0
    started: float = field(default_factory=time.monotonic)
    blocked: str | None = None

    def check(self):
        if self.blocked:
            raise ResearchError(self.blocked)
        if time.monotonic() - self.started >= self.seconds:
            self.blocked = "TIME_BUDGET"
            raise ResearchError(self.blocked)

    def consume(self, count):
        self.check()
        if self.used_bytes + count > self.max_bytes:
            self.blocked = "BYTE_BUDGET"
            raise ResearchError(self.blocked)
        self.used_bytes += count


@contextmanager
def sdk_session(budget):
    require(_SESSION_LOCK.acquire(blocking=False), "SDK_SESSION_BUSY")
    try:
        with _sdk_resources(budget) as sdk:
            yield sdk
    finally:
        _SESSION_LOCK.release()


@contextmanager
def _sdk_resources(budget):
    """Do not alter the user's persistent cache, SDK installation or identity."""
    require(bool(os.environ.get("EDGAR_IDENTITY", "").strip()), "IDENTITY_REQUIRED",
            "Provide the existing authorized EDGAR_IDENTITY in the process environment")
    try:
        version = importlib.metadata.version("edgartools")
    except importlib.metadata.PackageNotFoundError:
        raise ResearchError("SDK_UNAVAILABLE", "Use the existing edgartools interpreter") from None
    require(version == "5.62.0", "SDK_VERSION_UNVERIFIED",
            "This reader is validated against edgartools 5.62.0")
    old_environment = {key: os.environ.get(key) for key in
                       ("EDGAR_LOCAL_DATA_DIR", "EDGAR_RATE_LIMIT_PER_SEC", "EDGAR_HTTP_TIMEOUT")}
    with tempfile.TemporaryDirectory(prefix="sec-research-") as scratch:
        os.environ.update(EDGAR_LOCAL_DATA_DIR=scratch, EDGAR_RATE_LIMIT_PER_SEC="2",
                          EDGAR_HTTP_TIMEOUT=str(min(budget.seconds, 30)))
        previous, created = None, None
        try:
            import httpx
            import edgar
            from edgar import httpclient

            class CountedStream(httpx.SyncByteStream):
                def __init__(self, original):
                    self.original = original

                def __iter__(self):
                    for chunk in self.original:
                        budget.consume(len(chunk))
                        yield chunk

                def close(self):
                    self.original.close()

            def before_request(request):
                budget.check()
                require(request.method == "GET" and request.url.scheme == "https" and
                        request.url.host in {"www.sec.gov", "data.sec.gov"} and
                        request.url.port in {None, 443}, "PUBLIC_REQUEST_NOT_ALLOWED")

            def after_response(response):
                budget.check()
                if response.status_code in {401, 403}:
                    budget.blocked = "ACCESS_DENIED"
                    raise ResearchError("ACCESS_DENIED")
                if response.is_stream_consumed:
                    budget.consume(len(response.content))
                else:
                    response.stream = CountedStream(response.stream)

            previous = httpclient.HTTP_MGR
            created = httpclient.get_http_mgr(cache_enabled=False, request_per_sec_limit=2)
            httpclient.HTTP_MGR = created
            httpclient.configure_http(verify_ssl=True, timeout=min(budget.seconds, 30))
            httpclient.HTTP_MGR.httpx_params["event_hooks"] = {
                "request": [before_request], "response": [after_response]}
            yield edgar
        finally:
            cleanup_failed = False
            try:
                if created is not None:
                    created.close()
            except Exception:
                cleanup_failed = True
            finally:
                if previous is not None:
                    httpclient.HTTP_MGR = previous
                for key, value in old_environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            if cleanup_failed:
                raise ResearchError("SDK_CLEANUP_FAILED")
