"""Audited forward-adjusted daily close provider for correlation evidence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .futu_adapter import FutuReadError
from .repository import PrivateJsonReader
from .risk import PricePoint


class FutuAdjustedPriceProvider:
    def __init__(self, adapter: PrivateJsonReader) -> None:
        self.adapter = adapter

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
