"""Immutable workbench requests; calculation contracts remain in Tivelo."""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_serializer, model_validator
from tivelo_backtest.study_models import StudyRequest
from tivelo_contracts.base import Contract, Digest, RelativePath, fingerprint

from .preparation_models import CaptureRequest, LockedCapture, LockedPreparation, PreparationRequest

NodeId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
RequestKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


class ResearchNode(Contract):
    node_id: NodeId
    run: RelativePath | None = None
    snapshot: RelativePath | None = None
    study: StudyRequest | None = None
    depends_on: tuple[NodeId, ...] = ()
    prepare: PreparationRequest | None = None
    capture: CaptureRequest | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def input_mode(self) -> Self:
        if self.capture is not None:
            if any(
                value is not None for value in (self.prepare, self.run, self.snapshot, self.study)
            ):
                raise ValueError("a data capture node only accepts datasets and dependencies")
        elif self.prepare is None:
            if self.run is None or self.snapshot is None:
                raise ValueError("existing inputs require both run and snapshot")
        elif self.run is not None or self.snapshot is not None:
            raise ValueError("prepared inputs cannot also select an existing run or snapshot")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        payload = handler(self)
        if self.prepare is None:
            payload.pop("prepare", None)
        return payload


class ExecutionBudget(Contract):
    rss_limit_bytes: int = Field(default=6 * 1024**3, strict=True, ge=64 * 1024**2)
    timeout_seconds: int = Field(default=12 * 3600, strict=True, ge=1, le=12 * 3600)
    max_attempts: int = Field(default=3, strict=True, ge=1, le=10)


class JobRequest(Contract):
    request_version: Literal["1.0"] = "1.0"
    idempotency_key: RequestKey
    nodes: tuple[ResearchNode, ...] = Field(min_length=1, max_length=16)
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)

    @model_validator(mode="after")
    def dag(self) -> Self:
        ids = [node.node_id for node in self.nodes]
        if ids != sorted(set(ids)):
            raise ValueError("research nodes must have sorted unique identities")
        by_id = {node.node_id: node for node in self.nodes}
        visiting, visited = set(), set()

        def visit(identity):
            if identity in visiting:
                raise ValueError("cyclic research dependencies")
            if identity in visited:
                return
            visiting.add(identity)
            parents = by_id[identity].depends_on
            if list(parents) != sorted(set(parents)) or any(
                parent not in by_id for parent in parents
            ):
                raise ValueError("dependencies must be sorted, unique and present")
            for parent in parents:
                visit(parent)
            visiting.remove(identity)
            visited.add(identity)

        for identity in ids:
            visit(identity)
        if (
            sum(
                (
                    2 * len(node.capture.datasets) + 1
                    if node.capture
                    else 3 + (2 * len(node.prepare.datasets) + 2 if node.prepare else 0)
                )
                for node in self.nodes
            )
            > 96
        ):
            raise ValueError("research graph exceeds 96 execution stages")
        return self


class LockedInput(Contract):
    request: ResearchNode
    run_archive_id: Digest | None = None
    lock_id: Digest | None = None
    snapshot_id: Digest | None = None
    bundle_id: Digest | None = None
    preparation: LockedPreparation | LockedCapture | None = None

    @model_validator(mode="after")
    def input_mode(self) -> Self:
        references = (self.run_archive_id, self.lock_id, self.snapshot_id, self.bundle_id)
        specification = self.request.prepare or self.request.capture
        if specification is None:
            if self.preparation is not None or any(value is None for value in references):
                raise ValueError("existing input requires its complete content references")
        elif self.preparation is None or any(value is not None for value in references):
            raise ValueError("prepared input requires a preparation lock")
        elif isinstance(self.preparation, LockedPreparation) != (self.request.prepare is not None):
            raise ValueError("data capture and research preparation locks differ")
        elif tuple(source.source for source in self.preparation.sources) != specification.datasets:
            raise ValueError("preparation sources differ from the request")
        if isinstance(self.preparation, LockedPreparation):
            view = specification.run.price_view
            pin = self.preparation.price_extension
            if (pin is None) != (view.mode != "extension") or (
                pin and (pin.kind != "price_view" or pin.capability_id != view.provider_id)
            ):
                raise ValueError("prepared price view differs from the locked extension")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        payload = handler(self)
        if self.preparation is None:
            payload.pop("preparation", None)
        return payload


def plan_format(inputs):
    if any(
        spec.versions for item in inputs if (spec := item.request.prepare or item.request.capture)
    ):
        return "1.5"
    if any(
        (item.request.prepare and item.request.prepare.run.extension_fields)
        or (
            item.preparation
            and any(s.plan.request.extension_field for s in item.preparation.sources)
        )
        for item in inputs
    ):
        return "1.4"
    if any(s.plan.extension is not None for item in inputs if item.preparation for s in item.preparation.sources):
        return "1.3"
    if any(item.request.capture for item in inputs):
        return "1.2"
    if any(item.preparation for item in inputs):
        return "1.1"
    return "1.0"


class JobPlan(Contract):
    plan_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4", "1.5"] = "1.0"
    request: JobRequest
    inputs: tuple[LockedInput, ...]
    effective_budget: ExecutionBudget | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.plan_version < plan_format(self.inputs):
            raise ValueError("research inputs require a newer plan format")
        if tuple(item.request for item in self.inputs) != self.request.nodes:
            raise ValueError("locked inputs differ from the requested nodes")
        if self.effective_budget is not None and (
            self.effective_budget.rss_limit_bytes > self.request.budget.rss_limit_bytes
            or self.effective_budget.timeout_seconds != self.request.budget.timeout_seconds
            or self.effective_budget.max_attempts != self.request.budget.max_attempts
        ):
            raise ValueError("effective budget can only tighten the requested memory limit")
        return self

    @property
    def plan_id(self):
        return fingerprint(self)

    @property
    def budget(self):
        return self.effective_budget or self.request.budget

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        payload = handler(self)
        if self.effective_budget is None:
            payload.pop("effective_budget", None)
        return payload


class ArtifactReference(Contract):
    kind: Literal[
        "validation",
        "result",
        "study",
        "report",
        "pipeline",
        "source",
        "dataset",
        "snapshot",
        "preparation",
    ]
    identity: Digest
    path: RelativePath


class ValidationReceipt(Contract):
    format: Literal["tsp-engine-validation"] = "tsp-engine-validation"
    version: Literal["1.0"] = "1.0"
    plan_id: Digest
    node_id: str
    input: LockedInput


class ResearchReport(Contract):
    format: Literal["tsp-engine-report"] = "tsp-engine-report"
    version: Literal["1.0"] = "1.0"
    plan_id: Digest
    node_id: str
    execution: ArtifactReference
    summary: dict[str, Any]


class PipelineManifest(Contract):
    format: Literal["tsp-engine-pipeline"] = "tsp-engine-pipeline"
    version: Literal["1.0"] = "1.0"
    plan_id: Digest
    artifacts: dict[str, ArtifactReference]


def stages(plan):
    """Expand the real research subgraph; no arbitrary executable handlers."""
    output = {}
    final = {
        node.node_id: node.node_id + (".snapshot" if node.capture else ".report")
        for node in plan.request.nodes
    }
    for item in plan.inputs:
        name = item.request.node_id
        parents = tuple(final[parent] for parent in item.request.depends_on)
        if item.preparation is not None:
            published = []
            for index, _ in enumerate(item.preparation.sources):
                source = f"{name}.sync_{index:02d}"
                publication = f"{name}.publish_{index:02d}"
                output[source] = ("sync", item, parents)
                output[publication] = ("publish", item, (source,))
                published.append(publication)
            output[name + ".snapshot"] = ("snapshot", item, tuple(published) or parents)
            if item.request.capture is not None:
                continue
            output[name + ".prepare"] = ("prepare", item, (name + ".snapshot",))
            parents = (name + ".prepare",)
        output[name + ".validate"] = (
            "validate",
            item,
            parents,
        )
        output[name + ".execute"] = ("execute", item, (name + ".validate",))
        output[name + ".report"] = ("report", item, (name + ".execute",))
    return output
