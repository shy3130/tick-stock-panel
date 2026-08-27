import inspect

from app.data_providers.base import OrderedTransReaderFactory, ProviderCapabilities
from app.data_providers.fquant_provider import FQuantProvider


def test_only_fquant_declares_ordered_trans_factory_capability() -> None:
    assert ProviderCapabilities().ordered_trans_research is False
    assert FQuantProvider.capabilities.ordered_trans_research is True
    assert "raw_root" not in inspect.signature(FQuantProvider.open_ordered_trans_reader).parameters
    assert isinstance(FQuantProvider, type)
    assert callable(getattr(FQuantProvider, "open_ordered_trans_reader", None))
    assert OrderedTransReaderFactory is not None
