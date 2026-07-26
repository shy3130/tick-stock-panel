from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_head_shoulders_payload_survives_the_monitor_service_boundary() -> None:
    client = (
        ROOT / "backend/app/services/dow_monitor_client.py"
    ).read_text(encoding="utf-8")
    service = (
        ROOT / "backend/app/services/dow_monitor_service.py"
    ).read_text(encoding="utf-8")

    assert 'alias="headShoulders"' in client
    assert 'chart["headShoulders"]' in service


def test_head_shoulders_has_an_independent_chart_control_and_mapping() -> None:
    dialog = (
        ROOT
        / "frontend/src/components/dow-monitor/DowMonitorDetailDialog.tsx"
    ).read_text(encoding="utf-8")
    mapping = (
        ROOT / "frontend/src/components/dow-monitor/chartMappings.ts"
    ).read_text(encoding="utf-8")

    assert 'aria-label="头肩形态"' in dialog
    assert "toHeadShouldersOverlays" in mapping
    assert "NECKLINE_BREAK_WEAK" in mapping
