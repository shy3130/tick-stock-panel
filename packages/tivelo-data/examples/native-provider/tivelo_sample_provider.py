"""Community example compiled to a native module. All bundled observations are synthetic."""

import json
from datetime import UTC, datetime
from pathlib import Path

from tivelo_contracts.data import Bar, Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.providers import ProviderDescriptor

from tivelo_data.providers import FetchPage

VERSION = "1.0.0"
PACKAGE = "example.daily-feed"
CAPABILITY = "example.synthetic-bars"


def tivelo_probe():
    return dict(
        format="tivelo-native-probe-v1",
        id=PACKAGE,
        version=VERSION,
        kind="data-provider",
        contracts_api=2,
        extension_api=2,
        runtime_generation="tivelo-alpha1",
        capabilities=[CAPABILITY],
    )


def describe_provider(capability_id):
    if capability_id != CAPABILITY:
        raise ValueError("unknown capability")
    return ProviderDescriptor(
        provider_id=CAPABILITY,
        version=VERSION,
        source_ids=("example.archive",),
        capabilities=[
            dict(
                record_type="bar",
                market_id="cn.equity",
                asset_class="equity",
                timeframes=["1d"],
                price_bases=["raw"],
                knowledge_basis="historical_versions",
                normalization_version="example-bars-v1",
                bar_convention_id="cn.synthetic.day",
            )
        ],
    )


class ArchiveProvider:
    def __init__(self, plan, options, credential_ref):
        self.plan = plan
        self.options = options
        self.credential_ref = credential_ref
        self.rows = ()
        self.missing = ()

    def describe(self):
        return describe_provider(CAPABILITY)

    def check_ready(self, context):
        if self.options.get("require_token") and not context.resolve_credentials(
            self.credential_ref
        ).get("token"):
            raise TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Token required")
        source = Path(__file__).with_name("observations.json")
        request = self.plan.request
        if request.start < datetime(2025, 1, 2, tzinfo=UTC) or request.end > datetime(
            2025, 1, 4, tzinfo=UTC
        ):
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Sample covers the two published sessions",
            )
        rows = (Bar.model_validate(row) for row in json.loads(source.read_bytes()))
        self.rows = tuple(
            r
            for r in rows
            if r.instrument_id in request.instrument_ids
            and request.start <= r.bar_start < request.end
        )
        found = {row.instrument_id for row in self.rows}
        self.missing = tuple(i for i in request.instrument_ids if i not in found)

    def fetch(self, request, cursor, context):
        context.check_cancelled()
        index = int(cursor or 0)
        end = min(index + 1, len(self.rows))
        if end < len(self.rows):
            return FetchPage(self.rows[index:end], str(end))
        issues = tuple(
            CoverageIssue(
                instrument_id=key, reason="missing", detail="No sample rows in range"
            )
            for key in self.missing
        )
        observed = len(request.instrument_ids) - len(issues)
        coverage = Coverage(
            status="complete" if not issues else "partial" if observed else "empty",
            requested_units=len(request.instrument_ids),
            completed_units=observed,
            row_count=len(self.rows),
            issues=issues,
        )
        return FetchPage(self.rows[index:end], None, coverage)

    def close(self):
        self.rows = ()


def create_provider(*, plan, options, credential_ref, attempt_id, deployment):
    return ArchiveProvider(plan, options, credential_ref)
