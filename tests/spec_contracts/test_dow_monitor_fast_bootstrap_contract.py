from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_fast_bootstrap_spec_is_registered_with_stable_requirements() -> None:
    registry = (ROOT / "docs/spec-index.yaml").read_text(encoding="utf-8")
    traceability = (ROOT / "docs/traceability.yaml").read_text(encoding="utf-8")

    assert "SPEC-DOW-MONITOR-FAST-BOOTSTRAP-001" in registry
    for requirement in (
        "REQ-DOW-MONITOR-FAST-BOOTSTRAP-001",
        "REQ-DOW-MONITOR-LIGHTWEIGHT-LIST-OVERVIEW-001",
        "REQ-DOW-MONITOR-NOTIFICATION-SUMMARY-001",
        "REQ-DOW-MONITOR-STARTUP-PERFORMANCE-001",
    ):
        assert requirement in registry
        assert requirement in traceability


def test_fast_bootstrap_acceptance_and_review_documents_exist() -> None:
    assert (ROOT / "docs/acceptance/dow-monitor-fast-bootstrap.md").is_file()
    assert (
        ROOT / "docs/reviews/2026-08-02-dow-monitor-fast-bootstrap-review.md"
    ).is_file()
