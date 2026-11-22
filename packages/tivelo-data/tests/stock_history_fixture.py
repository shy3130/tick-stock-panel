"""Offline test double beneath the installed TdxAiData Python protocol binding."""

import json


def install_tdx_transport(tmp_path, monkeypatch, columns):
    from tivelo_data.legacy.tdx_runtime import LIB_DLL_NAME

    # Import the installed SDK's real Python binding. Replace only its native call with a wire fixture.
    # Each spawned child gets a synthetic library directory with no vendor INI or network connection.
    site = tmp_path / "fixture"
    pkg = site / "tdxaidata"
    lib = pkg / "lib"
    lib.mkdir(parents=True)
    (lib / LIB_DLL_NAME).write_bytes(b"offline-sdk-transport")
    request_log = tmp_path / "requests.jsonl"
    data = tmp_path / "reply.json"
    data.write_text(json.dumps({"ErrorId": 0, "Value": columns}))
    (pkg / "__init__.py").write_text(
        """import json, os, time
from configparser import ConfigParser
from pathlib import Path
from importlib.metadata import distribution
from types import SimpleNamespace
__path__.append(str(distribution("tdxaidata").locate_file("tdxaidata")))
from .tdxaidata import _NativeTdxAiData
class Offline:
    _dll = SimpleNamespace(GetHISDATsInStr="history", GetCWDATAInStr="events")
    _encode = staticmethod(_NativeTdxAiData._encode)
    get_cw_data = _NativeTdxAiData.get_cw_data
    get_his_dats = _NativeTdxAiData.get_his_dats
    def _call_with_free(self, method, *args):
        root = Path(os.environ["TDX_AI_DATA_LIB"]).parent
        config = ConfigParser()
        config.read(root/"TdxAiData.ini")
        key = config["Token"]["token"]
        assert key in ("local-tdx-a", "local-tdx-b")
        assert "UNRELATED_VENDOR_TEST_SECRET" not in os.environ
        assert root.resolve() == Path.cwd()
        with Path(LOG).open("a") as stream:
            stream.write(json.dumps([os.getpid(), str(root), [s.decode() if isinstance(s, bytes) else s for s in args]])+"\\n")
        value = json.loads(Path(DATA).read_text())
        if "replies" in value:
            value = value["replies"][method][args[0].decode()]
            if method == "history" and "by_end" in value:
                value = value["by_end"][args[2].decode()[:10]]
        if key == "local-tdx-b" and method == "history" and value.get("Value"):
            value["Value"]["Volume"] = [1319]
        if value.get("block"):
            time.sleep(60)
        return json.dumps(value)
_NativeTdxAiData.get_instance = classmethod(lambda cls: Offline())
"""
        + f"\nLOG={str(request_log)!r}\nDATA={str(data)!r}\n"
    )
    monkeypatch.syspath_prepend(str(site))
    return request_log, data
