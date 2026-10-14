"""Audited forward-adjusted daily close provider for correlation evidence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .futu_adapter import FutuReadError
from .repository import PrivateJsonReader
from .risk import PricePoint
from .models import PortfolioSnapshot
from .snapshot import normalize_security


class FutuAdjustedPriceProvider:
    def __init__(
        self, adapter: PrivateJsonReader, *, snapshots: Sequence[PortfolioSnapshot] = ()
    ) -> None:
        self.adapter = adapter
        source_codes: dict[str, set[str]] = {}
        for snapshot in snapshots:
            for position in snapshot.positions:
                if position.source_code is None:
                    continue
                if normalize_security(position.source_code) != (position.symbol, position.market):
                    raise FutuReadError("quote source code does not match the snapshot identity")
                identity = f"{position.market}.{position.symbol}"
                source_codes.setdefault(identity, set()).add(position.source_code)
        self._source_codes = {
            identity: next(iter(codes))
            for identity, codes in source_codes.items()
            if len(codes) == 1
        }

    def history(
        self,
        symbol: str,
        *,
        market: str,
        lookback: int,
        as_of: date,
        timeout_seconds: float = 5,
    ) -> tuple[PricePoint, ...] | None:
        code = f"{market.upper()}.{symbol.upper()}"
        if market.upper() == "CN":
            # Old or conflicting exchange evidence cannot safely route an A-share quote.
            source_code = self._source_codes.get(code)
            if source_code is None:
                return None
            code = source_code
        start = as_of - timedelta(days=max(lookback * 2, 180))
        payload = self.adapter.read_private_json(
            "get_kline",
            [
                code,
                "--ktype",
                "1d",
                "--start",
                start.isoformat(),
                "--end",
                as_of.isoformat(),
                "--rehab",
                "forward",
                "--session",
                "NONE",
                "--num",
                "1000",
                "--json",
            ],
            timeout=timeout_seconds,
        )
        if (
            payload.get("code") != code
            or payload.get("ktype") != "1d"
            or payload.get("source") != "history"
        ):
            raise FutuReadError("audited K-line response identity is invalid")
        records = payload.get("data")
        if not isinstance(records, list):
            raise FutuReadError("audited K-line response data is invalid")
        points: list[PricePoint] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise FutuReadError("audited K-line response contains an invalid row")
            try:
                day = datetime.strptime(str(record["time"]), "%Y-%m-%d %H:%M:%S").date()
                close = Decimal(str(record["close"]))
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise FutuReadError("audited K-line response contains an invalid row") from exc
            points.append(PricePoint(day, close))
        return tuple(points[-lookback:])
