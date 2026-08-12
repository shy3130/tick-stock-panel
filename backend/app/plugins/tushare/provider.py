"""Tushare Pro provider for auditable A-share data ingestion."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
import polars as pl

from app.data_providers.normalizer import cumulative_to_event_factors, normalize_daily, to_polars


@dataclass
class _TushareConfig:
    name: str = "tushare"
    display_name: str = "Tushare Pro (主数据源)"
    datasets: dict = field(
        default_factory=lambda: {
            "daily": None,
            "adj_factor": None,
            "instruments": None,
            "financial": None,
        }
    )
    path: None = None
    builtin: bool = True


def _yyyymmdd(value: date | datetime | None) -> str | None:
    return value.strftime("%Y%m%d") if value else None


_FINANCIAL_ENDPOINTS = {
    "metrics": ("fina_indicator", "fina_indicator_vip"),
    "income": ("income", "income_vip"),
    "balance_sheet": ("balancesheet", "balancesheet_vip"),
    "cash_flow": ("cashflow", "cashflow_vip"),
}

_FINANCIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "metrics": (
        "ts_code",
        "ann_date",
        "end_date",
        "eps",
        "dt_eps",
        "bps",
        "ocfps",
        "roe",
        "roe_waa",
        "roa",
        "grossprofit_margin",
        "netprofit_margin",
        "debt_to_assets",
        "q_sales_yoy",
        "or_yoy",
        "q_netprofit_yoy",
        "netprofit_yoy",
        "q_ocf_to_sales",
        "ocf_to_or",
        "inv_turn",
    ),
    "income": (
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "revenue",
        "oper_cost",
        "operate_profit",
        "sell_exp",
        "admin_exp",
        "rd_exp",
        "fin_exp",
        "non_oper_income",
        "non_oper_exp",
        "total_profit",
        "income_tax",
        "n_income",
        "n_income_attr_p",
        "basic_eps",
        "diluted_eps",
        "update_flag",
    ),
    "balance_sheet": (
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "total_assets",
        "total_cur_assets",
        "total_nca",
        "money_cap",
        "accounts_receiv",
        "inventories",
        "fix_assets",
        "intan_assets",
        "goodwill",
        "total_liab",
        "total_cur_liab",
        "total_ncl",
        "st_borr",
        "lt_borr",
        "acct_payable",
        "total_hldr_eqy_inc_min_int",
        "total_hldr_eqy_exc_min_int",
        "undistr_porfit",
        "minority_int",
        "update_flag",
    ),
    "cash_flow": (
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "n_cashflow_act",
        "n_cashflow_inv_act",
        "n_cash_flows_fnc_act",
        "c_pay_acq_const_fiolta",
        "n_incr_cash_cash_equ",
        "update_flag",
    ),
}

_FINANCIAL_MAPPINGS: dict[str, dict[str, tuple[str, ...]]] = {
    "metrics": {
        "eps_basic": ("eps",),
        "eps_diluted": ("dt_eps",),
        "bps": ("bps",),
        "ocfps": ("ocfps",),
        "roe": ("roe",),
        "roe_diluted": ("roe_waa",),
        "roa": ("roa",),
        "gross_margin": ("grossprofit_margin",),
        "net_margin": ("netprofit_margin",),
        "debt_to_asset_ratio": ("debt_to_assets",),
        "revenue_yoy": ("q_sales_yoy", "or_yoy"),
        "net_income_yoy": ("q_netprofit_yoy", "netprofit_yoy"),
        "operating_cash_to_revenue": ("q_ocf_to_sales", "ocf_to_or"),
        "inventory_turnover": ("inv_turn",),
    },
    "income": {
        "revenue": ("revenue",),
        "operating_cost": ("oper_cost",),
        "operating_profit": ("operate_profit",),
        "selling_expense": ("sell_exp",),
        "admin_expense": ("admin_exp",),
        "rd_expense": ("rd_exp",),
        "financial_expense": ("fin_exp",),
        "non_operating_income": ("non_oper_income",),
        "non_operating_expense": ("non_oper_exp",),
        "total_profit": ("total_profit",),
        "income_tax": ("income_tax",),
        "net_income": ("n_income",),
        "net_income_attributable": ("n_income_attr_p",),
        "basic_eps": ("basic_eps",),
        "diluted_eps": ("diluted_eps",),
    },
    "balance_sheet": {
        "total_assets": ("total_assets",),
        "total_current_assets": ("total_cur_assets",),
        "total_non_current_assets": ("total_nca",),
        "cash_and_equivalents": ("money_cap",),
        "accounts_receivable": ("accounts_receiv",),
        "inventory": ("inventories",),
        "fixed_assets": ("fix_assets",),
        "intangible_assets": ("intan_assets",),
        "goodwill": ("goodwill",),
        "total_liabilities": ("total_liab",),
        "total_current_liabilities": ("total_cur_liab",),
        "total_non_current_liabilities": ("total_ncl",),
        "short_term_borrowing": ("st_borr",),
        "long_term_borrowing": ("lt_borr",),
        "accounts_payable": ("acct_payable",),
        "total_equity": ("total_hldr_eqy_inc_min_int",),
        "equity_attributable": ("total_hldr_eqy_exc_min_int",),
        "retained_earnings": ("undistr_porfit",),
        "minority_interest": ("minority_int",),
    },
    "cash_flow": {
        "net_operating_cash_flow": ("n_cashflow_act",),
        "net_investing_cash_flow": ("n_cashflow_inv_act",),
        "net_financing_cash_flow": ("n_cash_flows_fnc_act",),
        "capex": ("c_pay_acq_const_fiolta",),
        "net_cash_change": ("n_incr_cash_cash_equ",),
    },
}

_SHARE_FIELDS = "ts_code,trade_date,total_share,float_share"


def _recent_quarter_ends(today: date, count: int = 5) -> list[date]:
    quarter_month = ((today.month - 1) // 3 + 1) * 3
    quarter_days = {3: 31, 6: 30, 9: 30, 12: 31}
    current = date(today.year, quarter_month, quarter_days[quarter_month])
    if current > today:
        previous_month = quarter_month - 3
        if previous_month:
            current = date(today.year, previous_month, quarter_days[previous_month])
        else:
            current = date(today.year - 1, 12, 31)

    result: list[date] = []
    for _ in range(count):
        result.append(current)
        previous_month = current.month - 3
        current = (
            date(current.year, previous_month, quarter_days[previous_month])
            if previous_month
            else date(current.year - 1, 12, 31)
        )
    return result


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


class TushareProvider:
    """Tushare Pro adapter. It never substitutes another data source."""

    name = "tushare"
    builtin = True

    _FULL_MARKET_THRESHOLD = 100

    def __init__(
        self,
        client=None,
        token: str | None = None,
        request_interval_seconds: float | None = None,
        today_fn: Callable[[], date] | None = None,
        share_history_years: int | None = None,
    ) -> None:
        self.config = _TushareConfig()
        self._today_fn = today_fn or date.today
        self._share_history_years = max(
            1,
            int(
                os.getenv("TUSHARE_SHARE_HISTORY_YEARS", "3")
                if share_history_years is None
                else share_history_years
            ),
        )
        self._last_request_at: float | None = None
        self._request_interval = (
            float(os.getenv("TUSHARE_REQUEST_INTERVAL_SECONDS", "0.35"))
            if request_interval_seconds is None and client is None
            else float(request_interval_seconds or 0)
        )
        if client is not None:
            self._client = client
            return
        resolved_token = token or os.getenv("TUSHARE_TOKEN")
        if not resolved_token:
            raise RuntimeError("TUSHARE_TOKEN is not configured")
        import tushare as ts

        self._client = ts.pro_api(resolved_token)

    def close(self) -> None:
        pass

    def _call(self, method, **kwargs):
        if self._request_interval > 0 and self._last_request_at is not None:
            remaining = self._request_interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        result = method(**kwargs)
        self._last_request_at = time.monotonic()
        return result

    def _open_trade_dates(
        self,
        start_time: date | datetime,
        end_time: date | datetime,
    ) -> list[str]:
        raw = self._call(
            self._client.trade_cal,
            exchange="",
            start_date=_yyyymmdd(start_time),
            end_date=_yyyymmdd(end_time),
            is_open="1",
            fields="cal_date,is_open",
        )
        if raw is None or raw.empty or "cal_date" not in raw.columns:
            return []
        return sorted({str(value) for value in raw["cal_date"].dropna().tolist()})

    def _normalize_daily_frame(
        self,
        raw,
        *,
        requested_symbols: set[str],
        default_symbol: str | None = None,
    ) -> pl.DataFrame:
        if raw is None or raw.empty:
            return pl.DataFrame()
        normalized_input = raw.copy()
        if "ts_code" in normalized_input.columns:
            normalized_input = normalized_input[
                normalized_input["ts_code"].astype(str).isin(requested_symbols)
            ]
        if normalized_input.empty:
            return pl.DataFrame()
        normalized_input["trade_date"] = pd.to_datetime(
            normalized_input["trade_date"],
            format="%Y%m%d",
            errors="coerce",
        ).dt.date
        normalized_input["amount"] = normalized_input["amount"] * 1000.0
        normalized_input["quote_ts"] = None
        return normalize_daily(
            normalized_input,
            default_symbol=default_symbol,
            source=self.name,
        )

    def _normalize_factor_frame(
        self,
        raw,
        *,
        requested_symbols: set[str],
        default_symbol: str | None = None,
    ) -> pl.DataFrame:
        if raw is None or raw.empty:
            return pl.DataFrame()
        normalized_input = raw.copy()
        if "ts_code" in normalized_input.columns:
            normalized_input = normalized_input[
                normalized_input["ts_code"].astype(str).isin(requested_symbols)
            ]
        if normalized_input.empty:
            return pl.DataFrame()
        normalized_input["trade_date"] = pd.to_datetime(
            normalized_input["trade_date"],
            format="%Y%m%d",
            errors="coerce",
        ).dt.date
        normalized_input = normalized_input.rename(columns={"ts_code": "symbol"})
        frame = to_polars(normalized_input)
        if "symbol" not in frame.columns and default_symbol:
            frame = frame.with_columns(pl.lit(default_symbol).alias("symbol"))
        return frame

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        if asset_type != "stock":
            return []
        fields = (
            "ts_code,symbol,name,exchange,market,list_status,list_date,delist_date"
        )
        rows: list[dict] = []
        exchange_map = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
        for status in ("L", "D", "P", "G"):
            frame = self._call(
                self._client.stock_basic,
                exchange="",
                list_status=status,
                fields=fields,
            )
            if frame is None or frame.empty:
                continue
            for item in frame.to_dict(orient="records"):
                symbol = str(item.get("ts_code") or "")
                if not symbol:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "name": item.get("name") or symbol,
                        "code": str(item.get("symbol") or symbol.split(".")[0]),
                        "exchange": exchange_map.get(
                            str(item.get("exchange") or ""),
                            symbol.rsplit(".", 1)[-1],
                        ),
                        "region": "CN",
                        "type": "stock",
                        "ext": {
                            "listing_date": item.get("list_date"),
                            "delist_date": item.get("delist_date"),
                            "list_status": item.get("list_status") or status,
                            "market": item.get("market"),
                        },
                    }
                )
        active_symbols = [
            row["symbol"]
            for row in rows
            if row["ext"].get("list_status") == "L"
        ]
        if active_symbols and hasattr(self._client, "daily_basic"):
            shares = self._get_share_capital(active_symbols, latest_only=True)
            if not shares.is_empty():
                share_map = {
                    row["symbol"]: row
                    for row in shares.select(
                        "symbol",
                        "total_shares",
                        "float_shares",
                    ).to_dicts()
                }
                for row in rows:
                    share_row = share_map.get(row["symbol"])
                    if share_row:
                        row["ext"]["total_shares"] = share_row["total_shares"]
                        row["ext"]["float_shares"] = share_row["float_shares"]
        return sorted(rows, key=lambda item: item["symbol"])

    def get_daily(
        self,
        symbols: list[str],
        start_time: date | datetime | None,
        end_time: date | datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        if asset_type != "stock":
            raise ValueError("TushareProvider currently supports stock daily data only")
        frames: list[pl.DataFrame] = []
        requested_symbols = set(symbols)
        use_trade_dates = (
            len(symbols) >= self._FULL_MARKET_THRESHOLD
            and start_time is not None
            and end_time is not None
        )
        if use_trade_dates:
            trade_dates = self._open_trade_dates(start_time, end_time)
            total = len(trade_dates)
            for index, trade_date in enumerate(trade_dates, start=1):
                raw = self._call(self._client.daily, trade_date=trade_date)
                frame = self._normalize_daily_frame(
                    raw,
                    requested_symbols=requested_symbols,
                )
                if not frame.is_empty():
                    frames.append(frame)
                if on_chunk_done:
                    on_chunk_done(index, total)
        else:
            total = len(symbols)
            for index, symbol in enumerate(symbols, start=1):
                raw = self._call(
                    self._client.daily,
                    ts_code=symbol,
                    start_date=_yyyymmdd(start_time),
                    end_date=_yyyymmdd(end_time),
                )
                frame = self._normalize_daily_frame(
                    raw,
                    requested_symbols=requested_symbols,
                    default_symbol=symbol,
                )
                if not frame.is_empty():
                    frames.append(frame)
                if on_chunk_done:
                    on_chunk_done(index, total)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: date | datetime | None,
        end_time: date | datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        if asset_type != "stock":
            raise ValueError("TushareProvider currently supports stock factors only")
        frames: list[pl.DataFrame] = []
        requested_symbols = set(symbols)
        use_trade_dates = (
            len(symbols) >= self._FULL_MARKET_THRESHOLD
            and start_time is not None
            and end_time is not None
        )
        if use_trade_dates:
            trade_dates = self._open_trade_dates(start_time, end_time)
            total = len(trade_dates)
            for index, trade_date in enumerate(trade_dates, start=1):
                raw = self._call(self._client.adj_factor, trade_date=trade_date)
                frame = self._normalize_factor_frame(
                    raw,
                    requested_symbols=requested_symbols,
                )
                if not frame.is_empty():
                    frames.append(frame)
                if on_chunk_done:
                    on_chunk_done(index, total)
        else:
            total = len(symbols)
            for index, symbol in enumerate(symbols, start=1):
                raw = self._call(
                    self._client.adj_factor,
                    ts_code=symbol,
                    start_date=_yyyymmdd(start_time),
                    end_date=_yyyymmdd(end_time),
                )
                frame = self._normalize_factor_frame(
                    raw,
                    requested_symbols=requested_symbols,
                    default_symbol=symbol,
                )
                if not frame.is_empty():
                    frames.append(frame)
                if on_chunk_done:
                    on_chunk_done(index, total)
        if not frames:
            return pl.DataFrame()
        return cumulative_to_event_factors(pl.concat(frames, how="diagonal_relaxed"))

    def _normalize_financial_frame(
        self,
        raw_frames: list[pd.DataFrame],
        *,
        table: str,
        requested_symbols: set[str],
        latest_only: bool,
    ) -> pl.DataFrame:
        if not raw_frames:
            return pl.DataFrame()
        raw = pd.concat(raw_frames, ignore_index=True, sort=False)
        if raw.empty or "ts_code" not in raw.columns or "end_date" not in raw.columns:
            return pl.DataFrame()
        raw = raw[raw["ts_code"].astype(str).isin(requested_symbols)].copy()
        if raw.empty:
            return pl.DataFrame()

        output = pd.DataFrame(index=raw.index)
        output["symbol"] = raw["ts_code"].astype(str)
        output["period_end"] = pd.to_datetime(
            raw["end_date"],
            format="%Y%m%d",
            errors="coerce",
        ).dt.date

        announce = None
        for column in ("f_ann_date", "ann_date"):
            if column in raw.columns:
                announce = (
                    raw[column]
                    if announce is None
                    else announce.combine_first(raw[column])
                )
        if announce is not None:
            output["announce_date"] = pd.to_datetime(
                announce,
                format="%Y%m%d",
                errors="coerce",
            ).dt.date
        else:
            output["announce_date"] = None

        for target, sources in _FINANCIAL_MAPPINGS[table].items():
            values = None
            for source in sources:
                if source in raw.columns:
                    values = (
                        raw[source]
                        if values is None
                        else values.combine_first(raw[source])
                    )
            if values is not None:
                output[target] = pd.to_numeric(values, errors="coerce")

        output = output.dropna(subset=["symbol", "period_end"])
        if output.empty:
            return pl.DataFrame()
        output["_announce_order"] = pd.to_datetime(
            output["announce_date"],
            errors="coerce",
        )
        output["_row_order"] = range(len(output))
        output = output.sort_values(
            ["symbol", "period_end", "_announce_order", "_row_order"],
            na_position="first",
        ).drop_duplicates(["symbol", "period_end"], keep="last")
        if latest_only:
            output = (
                output.sort_values(
                    ["symbol", "period_end", "_announce_order", "_row_order"],
                    na_position="first",
                )
                .groupby("symbol", sort=False, as_index=False)
                .tail(1)
            )
        output = output.drop(columns=["_announce_order", "_row_order"]).sort_values(
            ["symbol", "period_end"]
        )
        return pl.from_pandas(output.reset_index(drop=True))

    def _normalize_share_frame(
        self,
        raw_frames: list[pd.DataFrame],
        *,
        requested_symbols: set[str],
        latest_only: bool,
    ) -> pl.DataFrame:
        if not raw_frames:
            return pl.DataFrame()
        raw = pd.concat(raw_frames, ignore_index=True, sort=False)
        required = {"ts_code", "trade_date", "total_share", "float_share"}
        if raw.empty or not required <= set(raw.columns):
            return pl.DataFrame()
        raw = raw[raw["ts_code"].astype(str).isin(requested_symbols)].copy()
        if raw.empty:
            return pl.DataFrame()

        output = pd.DataFrame(index=raw.index)
        output["symbol"] = raw["ts_code"].astype(str)
        output["period_end"] = pd.to_datetime(
            raw["trade_date"],
            format="%Y%m%d",
            errors="coerce",
        ).dt.date
        output["announce_date"] = output["period_end"]
        output["total_shares"] = (
            pd.to_numeric(raw["total_share"], errors="coerce") * 10_000.0
        )
        output["float_shares"] = (
            pd.to_numeric(raw["float_share"], errors="coerce") * 10_000.0
        )
        output = (
            output.dropna(subset=["symbol", "period_end"])
            .sort_values(["symbol", "period_end"])
            .drop_duplicates(["symbol", "period_end"], keep="last")
        )
        if output.empty:
            return pl.DataFrame()
        if latest_only:
            output = output.groupby("symbol", sort=False, as_index=False).tail(1)
        else:
            previous_total = output.groupby("symbol")["total_shares"].shift(1)
            previous_float = output.groupby("symbol")["float_shares"].shift(1)
            first = output.groupby("symbol").cumcount() == 0
            unchanged_total = output["total_shares"].eq(previous_total) | (
                output["total_shares"].isna() & previous_total.isna()
            )
            unchanged_float = output["float_shares"].eq(previous_float) | (
                output["float_shares"].isna() & previous_float.isna()
            )
            output = output[first | ~(unchanged_total & unchanged_float)]
        return pl.from_pandas(
            output.sort_values(["symbol", "period_end"]).reset_index(drop=True)
        )

    def _get_share_capital(
        self,
        symbols: list[str],
        *,
        latest_only: bool,
    ) -> pl.DataFrame:
        requested_symbols = set(symbols)
        today = self._today_fn()
        start = (
            today - timedelta(days=45)
            if latest_only
            else _years_ago(today, self._share_history_years)
        )
        frames: list[pd.DataFrame] = []
        if len(symbols) >= self._FULL_MARKET_THRESHOLD:
            trade_dates = self._open_trade_dates(start, today)
            for trade_date in trade_dates:
                raw = self._call(
                    self._client.daily_basic,
                    trade_date=trade_date,
                    fields=_SHARE_FIELDS,
                )
                if raw is not None and not raw.empty:
                    frames.append(raw)
        else:
            for symbol in symbols:
                raw = self._call(
                    self._client.daily_basic,
                    ts_code=symbol,
                    start_date=_yyyymmdd(start),
                    end_date=_yyyymmdd(today),
                    fields=_SHARE_FIELDS,
                )
                if raw is not None and not raw.empty:
                    frames.append(raw)
        return self._normalize_share_frame(
            frames,
            requested_symbols=requested_symbols,
            latest_only=latest_only,
        )

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """Return point-in-time financial rows without substituting providers."""
        if not symbols:
            return pl.DataFrame()
        if table == "shares":
            return self._get_share_capital(symbols, latest_only=latest_only)
        if table not in _FINANCIAL_ENDPOINTS:
            raise ValueError(f"Unsupported Tushare financial table: {table}")

        requested_symbols = set(symbols)
        standard_name, vip_name = _FINANCIAL_ENDPOINTS[table]
        fields = ",".join(_FINANCIAL_FIELDS[table])
        frames: list[pd.DataFrame] = []
        if len(symbols) >= self._FULL_MARKET_THRESHOLD:
            method = getattr(self._client, vip_name)
            for period_end in _recent_quarter_ends(self._today_fn()):
                raw = self._call(
                    method,
                    period=_yyyymmdd(period_end),
                    fields=fields,
                )
                if raw is not None and not raw.empty:
                    frames.append(raw)
        else:
            method = getattr(self._client, standard_name)
            for symbol in symbols:
                raw = self._call(method, ts_code=symbol, fields=fields)
                if raw is not None and not raw.empty:
                    frames.append(raw)

        return self._normalize_financial_frame(
            frames,
            table=table,
            requested_symbols=requested_symbols,
            latest_only=latest_only,
        )
