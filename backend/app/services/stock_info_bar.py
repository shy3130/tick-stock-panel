"""Schema for stock detail information-bar preferences and their backups."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

NonEmpty = Annotated[str, Field(min_length=1)]


class _BuiltinSource(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    type: Literal["builtin", "computed"]
    key: NonEmpty


class _ExtSource(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, alias_generator=to_camel)

    type: Literal["ext"]
    config_id: NonEmpty
    field_name: NonEmpty
    field_label: str | None = None
    field_type: str | None = None


class _ExtDisplay(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False, alias_generator=to_camel)

    display_mode: Literal["tag", "text"] | None = None
    separator: str | None = None
    max_width: str | None = None
    max_tags: float | None = None
    hidden_indices: list[int] | None = None
    tag_layout: Literal["horizontal", "vertical"] | None = None
    thousand_separator: bool | None = None
    unit_convert: Literal["none", "wan", "yi", "auto"] | None = None
    unit_decimals: float | None = None
    snapshot_at: str | None = None
    snapshot_render: Literal["text", "bar", "line"] | None = None
    series_granularity: Literal["daily", "intraday"] | None = None
    chart_width: float | None = None
    chart_height: float | None = None
    chart_days: float | None = None


class _InfoField(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, alias_generator=to_camel)

    id: NonEmpty
    source: Annotated[_BuiltinSource | _ExtSource, Field(discriminator="type")]
    label: str
    visible: bool
    align: Literal["left", "center", "right"] | None = None
    standalone: bool | None = None
    ext_display: _ExtDisplay | None = None


class StockInfoBarLayout(BaseModel):
    model_config = ConfigDict(strict=True)

    fields: list[_InfoField]
    baseline: list[_InfoField] | None
