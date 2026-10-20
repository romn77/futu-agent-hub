"""Prepare, finish or abandon one explicitly requested cache-aware analysis."""

import argparse
import json

from ..current_analysis import CurrentAnalysisError, TemporaryCleanupError
from ..private_home import PrivateHomeError
from ..repository import AccountSelectionRequired, SourceDataError
from ..settings import Settings, SettingsError
from .cli import read_input
from .paths import stores
from .report import request_document
from .types import CacheError
from .workflow import CachedWorkflow


def parser():
    result = argparse.ArgumentParser(description="Cache-aware read-only portfolio analysis")
    result.add_argument("--request")
    result.add_argument("--context")
    result.add_argument("--report", help="Private JSON string containing the final Markdown")
    result.add_argument("--coverage")
    result.add_argument("--research-refs")
    result.add_argument("--abort", action="store_true")
    result.add_argument("--json", action="store_true")
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        if not args.request:
            raise CacheError("INVALID_REQUEST")
        request = request_document(read_input(args.request, private=True, limit=65536))
        settings = Settings.from_env()
        public, private = stores(settings.data_home, enabled=request["cache_mode"] != "off")
        workflow = CachedWorkflow(settings, private, public)
        if args.abort:
            result = workflow.abort(read_input(args.context, private=True))
        elif args.report:
            result = workflow.finalize(request, read_input(args.context, private=True),
                                       read_input(args.report, private=True, limit=2 * 1024 * 1024),
                                       read_input(args.coverage, private=True), read_input(args.research_refs))
        elif any((args.context, args.coverage, args.research_refs)):
            raise CacheError("INVALID_REQUEST")
        else:
            result = workflow.prepare(request)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (CacheError, CurrentAnalysisError, TemporaryCleanupError, PrivateHomeError,
            SettingsError, SourceDataError, ValueError, TypeError, OSError) as exc:
        code = (exc.code if isinstance(exc, CacheError) else
                "ACCOUNT_SELECTION_REQUIRED" if isinstance(exc, AccountSelectionRequired) else
                type(exc).__name__ if isinstance(exc, (CurrentAnalysisError, TemporaryCleanupError)) else
                "SOURCE_UNAVAILABLE" if isinstance(exc, SourceDataError) else "INVALID_REQUEST")
        print(json.dumps({"route": "error", "reason": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
