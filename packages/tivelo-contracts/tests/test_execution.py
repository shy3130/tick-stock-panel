import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tivelo_contracts import ErrorCode, TiveloError, require_api_major
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.capabilities import CapabilityFacts, resolve_capability
from tivelo_contracts.data import DatasetReference, SnapshotContents, SnapshotManifest
from tivelo_contracts.execution import (
    BacktestSpec,
    DefinitionReference,
    ExecutionModel,
    OrderEvent,
    PackageReference,
    Portfolio,
    PriceViewSpec,
    RunLock,
    RunLockContents,
    RuntimeReference,
    ScaleProof,
    StrategyContext,
)
from tivelo_contracts.extension_fields import FormulaField


@pytest.mark.parametrize(
    "change",
    [
        {"alias": "close"},
        {"alias": "ext_2rank"},
        {"alias": "ext_Rank"},
        {"value_type": "object"},
        {"value_type": "string"},
        {"equals_text": "up"},
        {"field_sha256": "bad"},
    ],
)
def test_formula_field_rejects_ambiguous_names_or_types(change):
    values = dict(dataset_id="desk.rank", field_sha256="e" * 64, alias="ext_rank", value_type="int")
    with pytest.raises(ValidationError):
        FormulaField(**{**values, **change})


def test_field_proof_binds_aliases_and_types_and_rejects_duplicate_identities():
    field = FormulaField(
        dataset_id="desk.phase",
        field_sha256="a" * 64,
        alias="ext_up",
        value_type="string",
        equals_text="up",
    )
    other = field.model_copy(update={"alias": "ext_down", "equals_text": "down"})
    common = dict(
        strategy=DefinitionReference(definition_id="desk.strategy", version="1", sha256="b" * 64),
        program_sha256="c" * 64,
    )
    v11 = dict(**common, verifier_version="tivelo.scale.controlled.v1.1")
    assert ScaleProof(**v11, field_operands=(field, other)).field_operands == (field, other)
    for operands in (
        (),
        (field, field),
        (field, other.model_copy(update={"field_sha256": "f" * 64})),
        (field, other.model_copy(update={"dataset_id": "another"})),
    ):
        with pytest.raises(ValidationError):
            ScaleProof(**v11, field_operands=operands)
    with pytest.raises(ValidationError):
        ScaleProof(**common, field_operands=(field,))
    assert b'"field_operands"' not in canonical_bytes(ScaleProof(**common))


def snapshot():
    return SnapshotManifest.create(
        SnapshotContents(datasets=(DatasetReference(dataset_id="bar.raw", version_id="a" * 64),))
    )


def specification(**changes):
    ref = DefinitionReference(definition_id="strategy.fixture", version="1", sha256="b" * 64)
    market = DefinitionReference(definition_id="market.fixture", version="1", sha256="c" * 64)
    account = DefinitionReference(definition_id="account.fixture", version="1", sha256="d" * 64)
    values = dict(
        semantics_version="strict.daily.v1",
        snapshot_id=snapshot().snapshot_id,
        universe=("security-1",),
        start="2025-01-01",
        end="2025-01-03",
        strategy=ref,
        market_profile=market,
        account_profile=account,
        price_view=PriceViewSpec(provider_id="tivelo.raw", mode="raw"),
        execution=ExecutionModel(timeframe="1d"),
        accounting_model="legacy_adjusted_curve",
        knowledge_requirement="historical_versions",
        initial_capital="100000",
        base_currency="CNY",
    )
    values.update(changes)
    return BacktestSpec(**values)


def test_b01_strict_close_signal_cannot_request_same_close_fill():
    with pytest.raises(ValidationError, match="later session"):
        specification(execution=ExecutionModel(timeframe="1d", fill_at="legacy_same_close"))
    legacy = specification(
        semantics_version="legacy.tsp.v1",
        execution=ExecutionModel(timeframe="1d", fill_at="legacy_same_close"),
    )
    assert legacy.semantics_version == "legacy.tsp.v1"


def test_optional_timing_defaults_preserve_existing_lock_hash_inputs():
    default = specification().model_dump(mode="json")
    assert "warmup_start" not in default
    assert "decision_delay_seconds" not in default["execution"]
    changed = specification(
        warmup_start="2024-12-01",
        execution=ExecutionModel(timeframe="1d", decision_delay_seconds=30),
    ).model_dump(mode="json")
    assert changed["warmup_start"] == "2024-12-01"
    assert changed["execution"]["decision_delay_seconds"] == 30
    with pytest.raises(ValidationError):
        specification(warmup_start="2025-01-02")


@pytest.mark.parametrize("mode", ["qfq", "hfq"])
def test_b05_b06_static_prices_require_a_stable_explicit_anchor(mode):
    with pytest.raises(ValidationError, match="fixed anchor"):
        PriceViewSpec(provider_id=f"tivelo.{mode}", mode=mode)
    spec = PriceViewSpec(
        provider_id=f"tivelo.{mode}",
        mode=mode,
        anchor_at="2020-01-01T00:00:00Z",
        factor_definition_id="fixture-v1",
        factor_baseline_id="listing",
    )
    assert spec.anchor_at.year == 2020


def test_strategy_view_cannot_contain_future_information(make_bar, make_instrument):
    values = dict(
        now="2025-01-02T06:59:00Z",
        history=(make_bar(),),
        instruments=(make_instrument(),),
        portfolio=Portfolio(cash=(), positions=()),
        parameters_json="{}",
    )
    with pytest.raises(ValidationError, match="unavailable"):
        StrategyContext(**values)
    values["now"] = "2025-01-02T07:00:00Z"
    context = StrategyContext(**values)
    assert not hasattr(context, "reader")
    assert isinstance(context.history, tuple)


def test_parameters_freeze_by_value_and_reject_duplicate_members():
    ref = DefinitionReference(
        definition_id="fixture",
        version="1",
        sha256="a" * 64,
        parameters_json='{"z":[1,2],"a":{"p":0.1}}',
    )
    assert ref.parameters_json == '{"a":{"p":0.1},"z":[1,2]}'
    for value in ('{"a":1,"a":2}', '{"p":NaN}', '{"p":1e1000}', "[]"):
        with pytest.raises(ValidationError):
            DefinitionReference(
                definition_id="fixture", version="1", sha256="a" * 64, parameters_json=value
            )


def test_run_lock_detects_parameter_and_package_tampering():
    lock = RunLock.create(
        RunLockContents(
            spec=specification(),
            snapshot=snapshot(),
            verifier_version="strict-v1",
            packages=(
                PackageReference(name="tivelo-backtest", version="0.1.0a1", sha256="e" * 64),
            ),
            runtime=RuntimeReference(
                python_version="3.11.14",
                python_abi="cp311",
                platform_tag="synthetic",
                dependency_lock_sha256="f" * 64,
            ),
        )
    )
    assert RunLock.model_validate_json(canonical_bytes(lock)) == lock
    assert "market_adapter" not in lock.contents.model_dump()
    raw = json.loads(canonical_bytes(lock))
    raw["contents"]["spec"]["initial_capital"] = "200000"
    with pytest.raises(ValidationError, match="fingerprint mismatch"):
        RunLock.model_validate(raw)
    raw = json.loads(canonical_bytes(lock))
    raw["contents"]["packages"][0]["sha256"] = "1" * 64
    with pytest.raises(ValidationError, match="fingerprint mismatch"):
        RunLock.model_validate(raw)


def test_snapshot_identity_rejects_mutation_and_duplicate_datasets():
    value = snapshot().model_dump(mode="json")
    value["contents"]["datasets"][0]["version_id"] = "b" * 64
    with pytest.raises(ValidationError, match="fingerprint mismatch"):
        SnapshotManifest.model_validate(value)
    ref = DatasetReference(dataset_id="bar.raw", version_id="a" * 64)
    with pytest.raises(ValidationError, match="unique and sorted"):
        SnapshotContents(datasets=(ref, ref))


def test_order_cannot_claim_fill_before_eligibility():
    values = dict(
        order_id="order-1",
        instrument_id="security-1",
        side="buy",
        status="filled",
        decision_at="2025-01-02T07:00:00Z",
        submitted_at="2025-01-02T07:00:00Z",
        eligible_at="2025-01-03T01:30:00Z",
        filled_at="2025-01-02T07:00:00Z",
        requested_quantity="100",
        filled_quantity="100",
        raw_fill_price="10",
        currency="CNY",
    )
    with pytest.raises(ValidationError, match="eligible"):
        OrderEvent(**values)
    values["filled_at"] = "2025-01-03T01:30:00Z"
    assert OrderEvent(**values).filled_at == datetime(2025, 1, 3, 1, 30, tzinfo=UTC)


def capability_facts(**changes):
    values = dict(
        feature_id="backtest.price_view.dynamic",
        installed=True,
        trusted=True,
        compatible=True,
        enabled=True,
        configured=True,
        licensed=True,
        license_expired=False,
        supports_market=True,
        data_ready=True,
        pending_restart=False,
        load_failed=False,
    )
    values.update(changes)
    return CapabilityFacts(**values)


@pytest.mark.parametrize(
    "changes,status",
    [
        ({}, "available"),
        ({"installed": False}, "not_installed"),
        ({"trusted": False}, "incompatible"),
        ({"compatible": False}, "incompatible"),
        ({"enabled": False}, "disabled"),
        ({"licensed": False}, "needs_license"),
        ({"license_expired": True}, "license_expired"),
        ({"configured": False}, "needs_configuration"),
        ({"data_ready": False}, "missing_data"),
        ({"supports_market": False}, "unsupported_market"),
        ({"pending_restart": True}, "pending_restart"),
        ({"load_failed": True}, "load_failed"),
    ],
)
def test_capability_is_intersection_not_just_an_installation_flag(changes, status):
    assert resolve_capability(capability_facts(**changes)).status == status


def test_all_capability_failure_reasons_remain_visible():
    capability = resolve_capability(capability_facts(licensed=False, data_ready=False))
    assert capability.status == "needs_license"
    assert capability.reasons == ("entitlement_missing", "data_requirements_unsatisfied")


def test_unknown_major_and_auth_failure_are_not_retryable_success():
    with pytest.raises(TiveloError) as raised:
        require_api_major(3)
    assert raised.value.code == ErrorCode.SCHEMA_UNSUPPORTED
    assert not raised.value.retryable
    error = TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Authentication failed")
    assert not error.retryable
    assert TiveloError(ErrorCode.RATE_LIMITED, "Try later", retry_after=10).retryable
    with pytest.raises(ValueError):
        TiveloError(ErrorCode.AUTHENTICATION_FAILED, "Authentication failed", retry_after=10)
