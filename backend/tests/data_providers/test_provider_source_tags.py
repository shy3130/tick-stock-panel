from app.data_providers.fquant_provider import FQuantProvider


class FakeFinancialFStore:
    def query(self, sql, params=None):  # noqa: ARG002
        return [{"code": "600519", "t_date": "2026-03-31"}]


def test_financial_source_uses_provider_name():
    provider = object.__new__(FQuantProvider)
    provider._fstore = FakeFinancialFStore()
    provider.name = "fquant_local"

    row = provider.get_financial("600519.SH", "income").to_dicts()[0]

    assert row["source"] == "fquant_local:fstore:income"
