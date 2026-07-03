from unittest.mock import patch

from app.data_providers.fquant.sina_tencent_client import (
    SinaTencentClient,
    parse_sina,
    parse_tencent,
    parse_tencent_depth,
    _to_exch_code,
)


TENCENT_FIXTURE = (
    # Real sample checked 2026-07-02:
    # v_sh600519="1~贵州茅台~600519~1203.00~1193.01~1193.01~50870...
    'v_sh600519="1~贵州茅台~600519~1193.01~1185.49~1180.10~42473~21000~21473~'
    + "~".join(["0"] * 24)
    + '~1196.80~1166.33~1193.01/42473/5033838080~42473~503383~'
    + "~".join(["0"] * 10)
    + '";'
)

SINA_FIXTURE = (
    # Real sample checked 2026-07-02:
    # var hq_str_sh600519="贵州茅台,1193.010,1193.010,1203.000...
    'var hq_str_sh600519="贵州茅台,1180.100,1185.490,1193.010,1196.800,1166.330,'
    '1192.900,1193.010,4247300,5033838080.000,'
    + ",".join(["0"] * 20)
    + ',2026-07-02,14:30:00,00";'
)


def test_parse_tencent():
    rows = parse_tencent(TENCENT_FIXTURE)
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1193.01
    assert row["prev_close"] == 1185.49
    assert row["volume"] == 42473 * 100


def test_parse_tencent_depth_preserves_zero_queue():
    parts = ["0"] * 49
    parts[1] = "测试"
    parts[2] = "600000"
    parts[9] = "10.00"
    parts[10] = "123"
    parts[19] = "10.01"
    parts[20] = "0"
    text = 'v_sh600000="' + "~".join(parts) + '";'

    row = parse_tencent_depth(text)["600000.SH"]

    assert row["bid_prices"][0] == 10.0
    assert row["bid_volumes"][0] == 123
    assert row["ask_prices"][0] == 10.01
    assert row["ask_volumes"][0] == 0


def test_parse_sina():
    rows = parse_sina(SINA_FIXTURE, ["sh600519"])
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1193.01
    assert row["volume"] == 4247300
    assert row["timestamp"] == "2026-07-02T14:30:00"


def test_batch_chunking():
    client = SinaTencentClient()
    with patch.object(client, "_http_get", return_value=None) as get:
        client.get_quotes([f"{600000 + i}.SH" for i in range(130)], prefer="tencent")
        assert get.call_count == 3


def test_partial_failure_keeps_success_rows():
    client = SinaTencentClient()
    with patch.object(client, "_http_get", side_effect=[TENCENT_FIXTURE, None]):
        rows = client.get_quotes(["600519.SH"] + [f"{600000 + i}.SH" for i in range(60)], prefer="tencent")
        assert any(row["symbol"] == "600519.SH" for row in rows)


def test_source_cooldown_after_three_failures():
    client = SinaTencentClient()
    client._record_failure("tencent")
    client._record_failure("tencent")
    assert client._source_available("tencent")

    client._record_failure("tencent")
    assert not client._source_available("tencent")

    client._record_success("tencent")
    assert client._source_available("tencent")


def test_depth_batch_chunking():
    client = SinaTencentClient()
    with patch.object(client, "_http_get", return_value=None) as get:
        client.get_depth([f"{600000 + i}.SH" for i in range(130)])
        assert get.call_count == 3


def test_hk_symbol_uses_hk_prefix_for_depth():
    assert _to_exch_code("00700.HK") == "hk00700"

    parts = ["0"] * 49
    parts[1] = "腾讯控股"
    parts[2] = "00700"
    parts[9] = "300.0"
    parts[10] = "100"
    parts[19] = "300.2"
    parts[20] = "200"
    text = 'v_hk00700="' + "~".join(parts) + '";'

    assert "00700.HK" in parse_tencent_depth(text)
