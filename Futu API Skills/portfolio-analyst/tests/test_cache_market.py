from datetime import date, timedelta

from portfolio_analyst.cache import CacheStore
from portfolio_analyst.cache.market import MarketCache, use_market_cache
from portfolio_analyst.price_history import FutuAdjustedPriceProvider
from portfolio_analyst.technical import FutuTechnicalHistoryProvider
from test_cache_core import NOW


class Quotes:
    def __init__(self):
        self.calls = []

    def read_private_json(self, name, args, *, timeout=5):
        self.calls.append((name, args))
        start = date.fromisoformat(args[args.index("--start") + 1])
        end = date.fromisoformat(args[args.index("--end") + 1])
        days = [(end - timedelta(days=i)) for i in range(10)]
        return {"code": args[0], "ktype": args[args.index("--ktype") + 1], "source": "history",
                "data": [{"time": f"{d} 00:00:00", "open": 99, "high": 102, "low": 98,
                          "close": 100, "volume": 1000} for d in sorted(days) if start <= d <= end]}


def test_risk_and_technical_share_ohlcv_with_same_basis(tmp_path):
    source = Quotes()
    market = MarketCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    with use_market_cache(market):
        risk = FutuAdjustedPriceProvider(source)
        technical = FutuTechnicalHistoryProvider(source)
    closes = risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 19))
    bars = technical.bars("AAA", market="US", interval="1d", as_of=date(2026, 10, 19))
    assert len(source.calls) == 1
    assert [p.adjusted_close for p in closes] == [b.close for b in bars]
    technical.bars("AAA", market="US", interval="1w", as_of=date(2026, 10, 19))
    assert len(source.calls) == 2


def test_later_cutoff_and_expired_revision_check_refetch(tmp_path):
    moment = [NOW]
    source = Quotes()
    market = MarketCache(CacheStore(tmp_path / "public", clock=lambda: moment[0]))
    with use_market_cache(market):
        risk = FutuAdjustedPriceProvider(source)
    risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 19))
    risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 20))
    assert len(source.calls) == 2
    moment[0] += timedelta(days=1)
    risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 20))
    assert len(source.calls) == 3


def test_source_with_close_only_does_not_poison_ohlcv_cache(tmp_path):
    class CloseOnly(Quotes):
        def read_private_json(self, *args, **kwargs):
            body = super().read_private_json(*args, **kwargs)
            for row in body["data"]:
                del row["volume"]
            return body
    source = CloseOnly()
    market = MarketCache(CacheStore(tmp_path / "public", clock=lambda: NOW))
    with use_market_cache(market):
        risk = FutuAdjustedPriceProvider(source)
    for _ in range(2):
        risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 19))
    assert len(source.calls) == 2


def test_legacy_provider_has_no_implicit_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_CACHE_HOME", str(tmp_path / "must-not-exist"))
    source = Quotes()
    risk = FutuAdjustedPriceProvider(source)
    for _ in range(2):
        risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 19))
    assert len(source.calls) == 2
    assert not (tmp_path / "must-not-exist").exists()


def test_cache_write_failure_does_not_repeat_successful_source_read(tmp_path):
    source = Quotes()
    market = MarketCache(CacheStore(tmp_path / "public", clock=lambda: NOW, max_item_bytes=10))
    with use_market_cache(market):
        risk = FutuAdjustedPriceProvider(source)
    assert risk.history("AAA", market="US", lookback=120, as_of=date(2026, 10, 19))
    assert len(source.calls) == 1
    assert "TOO_LARGE" in market.diagnostics
