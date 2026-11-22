"""Read immutable daily prices for the ordinary TSP strategy service."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
from tivelo_backtest.legacy.snapshot_daily import read_legacy_daily_panel
from tivelo_backtest.price_views import StaticPriceView
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import TiveloError
from tivelo_contracts.execution import PriceViewSpec
from tivelo_data.adapters.cn_factors import DEFINITIONS

from app.market_time import CN_TZ
from app.services.engine_jobs.daily_snapshot import (
    DailySnapshotRef,
    DailySnapshotScope,
    daily_snapshot_scope,
    read_published_snapshot,
)
from app.services.engine_jobs.storage import JobError, JobStore


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=CN_TZ)


@dataclass(frozen=True)
class SnapshotDailySource:
    ref: DailySnapshotRef
    scope: DailySnapshotScope
    path: Path
    as_of: datetime

    @classmethod
    def open(cls, data_dir: Path, ref: DailySnapshotRef):
        try:
            store = JobStore(data_dir)
            artifact, bundle, locked = read_published_snapshot(store, ref.job_id, ref.node_id)
            if bundle.snapshot.snapshot_id != ref.snapshot_id:
                raise ValueError("快照标识已改变，请从引擎研究重新打开")
            scope = daily_snapshot_scope(locked, bundle)
            if scope is None:
                raise ValueError("快照需要同一范围的原始日K与完整复权因子")
            stage = store.get(ref.job_id)["nodes"][ref.node_id + ".snapshot"]
            as_of = datetime.fromisoformat(stage["attempts"][-1]["finished_at"])
            return cls(ref, scope, store.path(artifact.path), as_of)
        except JobError as exc:
            raise ValueError(f"无法打开回测快照。{exc}") from exc
        except (OSError, TiveloError) as exc:
            raise ValueError("回测快照缺失或校验失败，请检查研究任务") from exc

    def validate_config(self, config) -> None:
        if config.asset_type != "stock" or config.price_basis != "qfq":
            raise ValueError("此快照支持 A 股前复权日线回测")
        if config.minute_fill or config.exit_fill == "signal_next_minute":
            raise ValueError("日线快照缺少分钟K，请选择日线成交方式")
        if not self.scope.start <= config.start <= config.end <= self.scope.end:
            raise ValueError("回测日期必须在快照范围内")
        if config.symbols and set(config.symbols) - set(self.scope.symbols.values()):
            raise ValueError("回测标的必须在快照范围内")

    def read_panel(self, repo, symbols, start, end, fields) -> pl.DataFrame:
        bindings = self.scope.symbols
        if symbols:
            wanted_symbols = set(symbols)
            bindings = {key: symbol for key, symbol in bindings.items() if symbol in wanted_symbols}
        source = self.scope.factors
        price_view = PriceViewSpec(
            mode="qfq",
            provider_id=StaticPriceView("qfq").provider_id,
            anchor_at=_midnight(self.scope.end),
            factor_definition_id=DEFINITIONS[source.representation],
            factor_baseline_id=f"v1:{source.expected_provider}:{source.baseline_date}",
        )
        query = BarQuery(
            instrument_ids=tuple(bindings),
            timeframe="1d",
            price_basis="raw",
            start=_midnight(max(start, self.scope.start)),
            end=_midnight(min(end, self.scope.end) + timedelta(days=1)),
            as_of=self.as_of,
        )
        try:
            with PortableSnapshotReader(self.path, snapshot_id=self.ref.snapshot_id) as reader:
                panel = read_legacy_daily_panel(reader, query, symbols=bindings, price_view=price_view)
        except TiveloError as exc:
            raise ValueError("固定日K或复权因子校验失败，请检查研究任务") from exc
        missing = set(bindings.values()) - set(panel["symbol"].unique())
        if missing:
            raise ValueError(f"所选区间缺少部分标的的日K（{', '.join(sorted(missing))}）")

        # These enriched columns are calculated from pinned bars and the usual
        # workspace instrument table. No current daily/factor partitions are read.
        derived = set(fields) & {"turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs"}
        if derived:
            from app.indicators.pipeline import compute_limit_signals

            instruments = repo.get_instruments_asset("stock")
            if instruments.is_empty():
                raise ValueError("策略需要股本或涨跌停信息，请先补齐本地标的资料")
            shares = repo.get_historical_shares() if "turnover_rate" in derived else None
            panel = compute_limit_signals(panel, instruments, needed=derived, historical_shares=shares)
        return panel
