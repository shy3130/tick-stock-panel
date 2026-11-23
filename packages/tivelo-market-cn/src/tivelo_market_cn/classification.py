"""Explicit CN equity classifications supplied by historical data adapters."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator
from tivelo_contracts.base import Contract, fingerprint
from tivelo_contracts.data import Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError

from .presets import Venue

Board = Literal["main", "star", "chinext", "beijing"]


class CNEquityClass(Contract):
    class_format: Literal["tivelo-cn-equity-class-v1"] = "tivelo-cn-equity-class-v1"
    venue_id: Venue
    instrument_type: Literal["common_stock"] = "common_stock"
    board: Board
    risk_status: Literal["normal", "risk_warning"]
    listing_phase: Literal[
        "regular", "ipo_first_day", "ipo_first_five", "relisting_first", "delisting_first", "delisting"
    ]

    @model_validator(mode="after")
    def venue_board(self) -> Self:
        if (
            (self.board == "star" and self.venue_id != "XSHG")
            or (self.board == "chinext" and self.venue_id != "XSHE")
            or ((self.board == "beijing") != (self.venue_id == "XBSE"))
        ):
            raise ValueError("board and venue are incompatible")
        if (self.listing_phase == "ipo_first_day" and self.venue_id != "XBSE") or (
            self.listing_phase == "ipo_first_five" and self.venue_id == "XBSE"
        ):
            raise ValueError("listing phase and venue are incompatible")
        return self

    @property
    def spec_revision(self) -> str:
        return f"cn.equity.class.{fingerprint(self)}"

    def bind(self, instrument: Instrument) -> Instrument:
        """Normalize one historical record using explicit contemporaneous metadata.

        The adapter must supply a classification valid for this whole record.
        This function neither infers it from names/codes nor invents its history.
        """
        classification = CNEquityClass.model_validate_json(self.model_dump_json())
        instrument = Instrument.model_validate_json(instrument.model_dump_json())
        if (
            instrument.market_id != "cn.equity"
            or instrument.asset_class != "equity"
            or instrument.venue_id != classification.venue_id
            or instrument.instrument_type != classification.instrument_type
            or instrument.quote_currency != "CNY"
            or instrument.settlement_currency != "CNY"
            or instrument.timezone != "Asia/Shanghai"
        ):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_ASSET, "Historical instrument does not match the CN class"
            )
        return instrument.model_copy(update={"spec_revision": classification.spec_revision})
