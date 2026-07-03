"""Trade Journal 数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field

CASH_KINDS = ("dividend", "dividend_tax", "transfer_in", "transfer_out", "repo", "other")


@dataclass(frozen=True)
class Fill:
    date: str
    time: str
    symbol: str
    name: str
    side: str
    qty: float
    price: float
    amount: float
    fee: float
    account_id: str = "default"


@dataclass(frozen=True)
class CashEvent:
    date: str
    symbol: str
    kind: str
    amount: float
    account_id: str = "default"


@dataclass
class Roundtrip:
    symbol: str
    name: str
    open_date: str
    close_date: str
    qty: float
    buy_net: float
    sell_net: float
    fees: float
    dividend: float
    holding_days: int
    account_id: str = "default"

    @property
    def pnl(self) -> float:
        return self.sell_net - self.buy_net

    @property
    def total_pnl(self) -> float:
        return self.pnl + self.dividend

    @property
    def pnl_pct(self) -> float:
        return self.total_pnl / self.buy_net if self.buy_net else 0.0

    @property
    def buy_avg(self) -> float:
        return self.buy_net / self.qty if self.qty else 0.0

    @property
    def sell_avg(self) -> float:
        return self.sell_net / self.qty if self.qty else 0.0


@dataclass
class LedgerSummary:
    total_trips: int = 0
    win_trips: int = 0
    total_pnl: float = 0.0
    total_dividend: float = 0.0
    total_fees: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    open_positions: list[dict] = field(default_factory=list)
