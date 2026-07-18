def test_structure_breakout_core_is_importable() -> None:
    from longbridge_stock.structure_breakout_scanner import scan_history

    assert callable(scan_history)
