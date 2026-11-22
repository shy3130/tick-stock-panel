"""Derive a new immutable input from an archived run without executing strategy code."""

from pydantic import Field
from tivelo_contracts.base import Contract
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import PriceViewSpec, RunLock, RunLockContents

from .archives import read_run
from .controlled import check_controlled_spec, derive_program_bundle, load_controlled
from .definitions import CheckedDefinition, CheckedDefinitions
from .runner import run_requirements
from .runtime import CapturedRuntime, capture_runtime, verify_runtime


class PriceVariantRequest(Contract):
    price_view: PriceViewSpec
    adapt_controlled: bool = Field(default=False, strict=True)


def derive_price_variant(
    run, request: PriceVariantRequest, *, extension_runtime=None, expected_lock_id=None
):
    """Return the target lock and sealed definitions. Admission happens in the executing worker."""
    base, definitions = read_run(run)
    if expected_lock_id is not None and base.lock_id != expected_lock_id:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Source run changed before the variant was saved"
        )
    original = base.contents
    adapter = original.market_adapter
    if adapter is None or original.spec.semantics_version != "strict.daily.v1":
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Price variants require a strict daily run with a market adapter",
        )
    view = request.price_view
    pins, requirements = (), ()
    if view.mode == "extension":
        if extension_runtime is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "The selected price view needs an extension installation",
            )
        if view.provider_id == "tivelo.pro.dynamic" and (
            view.anchor_at is not None
            or not view.factor_definition_id
            or not view.factor_baseline_id
        ):
            raise TiveloError(
                ErrorCode.FACTOR_INCOMPATIBLE,
                "Dynamic prices need an explicit cumulative factor and baseline",
            )
        pins = (extension_runtime.resolve(view.provider_id),)
        requirements = tuple(sorted(set(getattr(extension_runtime, "software_requirements", ()))))
    elif view.provider_id != f"tivelo.{view.mode}":
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unknown built-in price provider")
    previous = CapturedRuntime(original.packages, original.runtime)
    verify_runtime(
        previous, run_requirements(definitions, adapter, original.extension_requirements)
    )
    runtime = (
        previous
        if original.extension_requirements == requirements
        else capture_runtime(run_requirements(definitions, adapter, requirements))
    )
    proof = original.scale_proof
    node = definitions.nodes[-1]
    if node.bundle.contents.language == "controlled_daily_v1":
        compiled = load_controlled(definitions).implementation
        if compiled.proof != proof:
            raise TiveloError(
                ErrorCode.PRICE_BASIS_UNPROVEN,
                "The archived strategy scale proof differs from its definition",
            )
        semantics = (
            "projected_prices_float64_v1"
            if view.mode == "extension"
            else "anchor_cancelled_float64_v1"
        )
        if compiled.program.numeric_semantics != semantics:
            if not request.adapt_controlled:
                raise TiveloError(
                    ErrorCode.PRICE_BASIS_UNPROVEN,
                    "Explicitly allow a derived controlled strategy for the new price semantics",
                )
            program = compiled.program.model_copy(update={"numeric_semantics": semantics})
            bundle, root, payload = derive_program_bundle(
                node.bundle, program, version_prefix="price-view"
            )
            definitions = CheckedDefinitions(
                root, (CheckedDefinition(bundle, root, ((bundle.contents.entrypoint, payload),)),)
            )
            proof = load_controlled(definitions).implementation.proof
    elif proof is not None:
        raise TiveloError(
            ErrorCode.PRICE_BASIS_UNPROVEN,
            "A trusted Python strategy cannot carry a controlled program proof",
        )
    spec = original.spec.model_copy(update={"price_view": view, "strategy": definitions.root})
    if proof is not None:
        check_controlled_spec(spec, proof)
    contents = original.model_dump()
    contents.update(
        spec=spec,
        scale_proof=proof,
        packages=runtime.packages,
        runtime=runtime.reference,
        extensions=pins,
        extension_requirements=requirements,
    )
    return RunLock.create(RunLockContents.model_validate(contents)), definitions
