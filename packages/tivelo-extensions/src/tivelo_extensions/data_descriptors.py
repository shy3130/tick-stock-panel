"""Read a data provider's catalog without importing its native module."""

from typing import Literal

from pydantic import Field
from tivelo_contracts.base import Contract, parse_json
from tivelo_contracts.extension_archive import read_engine_archive
from tivelo_contracts.providers import ProviderDescriptor

CATALOG = "payload/providers.json"


class ProviderCatalog(Contract):
    format: Literal["tivelo-dataset-providers-v1"]
    providers: tuple[ProviderDescriptor, ...] = Field(min_length=1, max_length=64)


def data_descriptors(data, release):
    raw = read_engine_archive(data).get(CATALOG)
    if not raw or len(raw) > 1_048_576:
        raise ValueError("数据源包缺少有效的已签名能力目录 payload/providers.json")
    try:
        catalog = ProviderCatalog.model_validate(parse_json(raw.decode("utf-8")))
        if [p.provider_id for p in catalog.providers] != [c.id for c in release.provides]:
            raise ValueError
        for provider in catalog.providers:
            if provider.version != release.version or len(set(provider.source_ids)) != len(
                provider.source_ids
            ):
                raise ValueError
            scopes = set()
            for cap in provider.capabilities:
                if cap.record_type == "bar":
                    if not cap.timeframes or not cap.price_bases or not cap.bar_convention_id:
                        raise ValueError
                    pairs = ((t, b) for t in cap.timeframes for b in cap.price_bases)
                else:
                    if cap.timeframes or cap.price_bases or cap.bar_convention_id:
                        raise ValueError
                    pairs = [(None, None)]
                for timeframe, basis in pairs:
                    scope = (cap.record_type, cap.market_id, cap.asset_class, timeframe, basis)
                    if scope in scopes:
                        raise ValueError
                    scopes.add(scope)
    except (UnicodeError, TypeError, ValueError):
        raise ValueError("数据源能力目录与签名版本、范围或标识不符") from None
    return {descriptor.provider_id: descriptor for descriptor in catalog.providers}
