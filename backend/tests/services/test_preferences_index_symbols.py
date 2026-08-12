from app.services import preferences


def test_sidebar_index_symbols_normalizes_legacy_values(monkeypatch):
    monkeypatch.setattr(
        preferences,
        "load",
        lambda: {
            "sidebar_index_symbols": [
                "000001.SH",
                "399001.SZ",
                "000001.INDEX",
                "not-an-index",
            ],
        },
    )

    assert preferences.get_sidebar_index_symbols() == [
        "000001.INDEX",
        "399001.INDEX",
    ]


def test_sidebar_index_symbols_accepts_legacy_string(monkeypatch):
    monkeypatch.setattr(
        preferences,
        "load",
        lambda: {"sidebar_index_symbols": "399006.SZ, 000680.SH"},
    )

    assert preferences.get_sidebar_index_symbols() == [
        "399006.INDEX",
        "000680.INDEX",
    ]
