import pytest

from tivelo_contracts.conformance import reader_fixture, verify_reader
from tivelo_contracts.testing import InMemoryReader


def test_published_queries_match_the_reference_reader():
    reader = InMemoryReader(**reader_fixture(), knowledge_basis="historical_versions")
    assert len(verify_reader(reader)) == 15
    assert len(verify_reader(reader, execution=True)) == 18


def test_vendor_reading_the_latest_revision_fails_at_its_first_early_query():
    class LatestOnly(InMemoryReader):
        def bars(self, query):
            return super().bars(query.model_copy(update={"as_of": query.end}))

    reader = LatestOnly(**reader_fixture(), knowledge_basis="historical_versions")
    with pytest.raises(AssertionError, match="bar-before-publication"):
        verify_reader(reader)
