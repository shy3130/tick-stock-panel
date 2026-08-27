from datetime import date

from app.services.weak_to_strong_research_data import WeakToStrongProductionReader


class _Child:
    def __init__(self, generation="g"):
        self.closed = 0
        self._generation = generation

    def generation(self):
        return self._generation

    def manifest_sha256(self):
        return "a" * 64

    def close(self):
        self.closed += 1


def test_unavailable_microstructure_is_explicit_and_close_cascades():
    canonical = _Child()
    canonical.manifest = lambda: {"created_at": "2026-01-01T00:00:00+00:00"}
    canonical.market_days = lambda start, end: [date(2026, 1, 8)]
    canonical.daily_bars = lambda symbol, start, end: []
    markets = _Child()
    markets.created_at = lambda: "2026-01-01T00:00:00+00:00"
    markets.market_days = lambda start, end: [date(2026, 1, 8)]
    markets.limit_regime_facts = lambda *args: {}
    minute = _Child()
    minute.catalog_manifest = lambda: {"coverage": {"600000.SH": ["2026-01-08"]}}
    minute.minute_bars = lambda *args: []
    reader = WeakToStrongProductionReader(canonical, markets, minute, None)
    assert reader.ticks("600000.SH", date(2026, 1, 8)) == ()
    assert reader.order_book_snapshots("600000.SH", date(2026, 1, 8)) == ()
    assert reader.pit_snapshot("600000.SH", date(2026, 1, 8)) is None
    assert "sortable_tick_reader" in set(__import__("app.services.weak_to_strong", fromlist=["FULL_CAPABILITIES"]).FULL_CAPABILITIES) - set(reader.capabilities())
    reader.close()
    reader.close()
    assert canonical.closed == markets.closed == minute.closed == 1


def test_production_adapter_normalizes_canonical_and_minute_symbols():
    class Frame:
        def to_dicts(self):
            return []

    class Canonical(_Child):
        def __init__(self):
            super().__init__()
            self.seen = []
        def manifest(self):
            return {}
        def market_days(self, start, end):
            return []
        def daily_bars(self, symbol, start, end):
            self.seen.append(symbol)
            return Frame()

    class Minute(_Child):
        def __init__(self):
            super().__init__()
            self.seen = []
            self.catalog_manifest = lambda: {}
        def minute_bars(self, symbol, day):
            self.seen.append(symbol)
            return []

    canonical, minute = Canonical(), Minute()
    markets = _Child()
    markets.created_at = lambda: None
    markets.market_days = lambda start, end: []
    reader = WeakToStrongProductionReader(canonical, markets, minute, None)
    reader.daily_bars("600000", date(2026, 1, 1), date(2026, 1, 2))
    reader.minute_bars("600000", date(2026, 1, 1))
    assert canonical.seen == ["600000.SH"]
    assert minute.seen == ["600000.SH"]
