"""只读研究示例: 实际 stdio 服务器 + 隔离的开放网关 fixture。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import research


class Gateway(BaseHTTPRequestHandler):
    status = 200
    rows: ClassVar[list] = [{"symbol": "600519.SH", "name": "贵州茅台"}]
    requests: ClassVar[list] = []

    def do_GET(self):
        self.requests.append((self.path, self.headers.get("Authorization")))
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"results": self.rows}).encode())

    def log_message(self, *_args):
        pass


class ResearchTests(unittest.TestCase):
    def setUp(self):
        Gateway.status = 200
        Gateway.rows = [{"symbol": "600519.SH", "name": "贵州茅台"}]
        Gateway.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = patch.dict(os.environ, {
            "TSP_TOKEN": "tsp_fixture",
            "TSP_BASE": f"http://127.0.0.1:{self.server.server_port}",
            "TSP_SCOPES": "write:ext",  # 示例必须只暴露 read:market。
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_real_stdio_lookup_exact_symbol(self):
        Gateway.rows.insert(0, {"symbol": "000001.SZ", "name": "平安银行"})
        instrument = asyncio.run(research.lookup_instrument("600519.SH"))
        self.assertEqual(instrument, {"symbol": "600519.SH", "name": "贵州茅台"})
        path, auth = Gateway.requests[0]
        self.assertEqual(urlsplit(path).path, "/api/kline/instruments/search")
        self.assertEqual(parse_qs(urlsplit(path).query), {"q": ["600519.SH"], "limit": ["8"]})
        self.assertEqual(auth, "Bearer tsp_fixture")
        self.assertEqual(os.environ["TSP_SCOPES"], "write:ext")

    def test_empty_lookup_stops(self):
        Gateway.rows = []
        with self.assertRaisesRegex(RuntimeError, "未找到"):
            asyncio.run(research.lookup_instrument("600519.SH"))

    def test_gateway_denial_stops(self):
        Gateway.status = 403
        with self.assertRaises(RuntimeError):
            asyncio.run(research.lookup_instrument("600519.SH"))

    def test_missing_token_stops_before_request(self):
        with patch.dict(os.environ, {"TSP_TOKEN": ""}), self.assertRaisesRegex(RuntimeError, "TSP_TOKEN"):
            asyncio.run(research.lookup_instrument("600519.SH"))
        self.assertEqual(Gateway.requests, [])

    def test_decode_structured_and_text_results(self):
        structured = SimpleNamespace(is_error=False, structured_content={"results": []})
        self.assertEqual(research.decode_result(structured), {"results": []})
        text = SimpleNamespace(is_error=False, structured_content=None,
                               content=[SimpleNamespace(type="text", text='{"results": []}')])
        self.assertEqual(research.decode_result(text), {"results": []})

    def test_tool_errors_and_invalid_json_stop(self):
        for result in [
            SimpleNamespace(is_error=True),
            SimpleNamespace(is_error=False, structured_content=None,
                            content=[SimpleNamespace(type="text", text="HTTP 403")]),
        ]:
            with self.assertRaises(RuntimeError):
                research.decode_result(result)

    def test_search_fetch_use_anonymous_transport_and_one_session(self):
        calls = []

        @asynccontextmanager
        async def transport(url, *, http_client):
            self.assertEqual(url, "https://search.parallel.ai/mcp")
            self.assertEqual(http_client.headers["User-Agent"], research.USER_AGENT)
            self.assertNotIn("Authorization", http_client.headers)
            yield None, None

        session = AsyncMock()
        session.list_tools.return_value = SimpleNamespace(
            tools=[SimpleNamespace(name="web_search"), SimpleNamespace(name="web_fetch")],
        )

        async def call(name, args):
            calls.append((name, args))
            return SimpleNamespace(is_error=False, structured_content={"results": []})

        session.call_tool.side_effect = call
        context = AsyncMock()
        context.__aenter__.return_value = session
        with (
            patch.object(research, "streamable_http_client", transport),
            patch.object(research, "ClientSession", return_value=context),
            patch.dict(os.environ, {"PARALLEL_API_KEY": "must-not-be-used"}),
        ):
            result = asyncio.run(research.search_web(
                {"symbol": "600519.SH", "name": "贵州茅台"}, "年度报告", "https://example.com/",
            ))
        self.assertEqual(result["search"], {"results": []})
        self.assertEqual(result["fetch"], {"results": []})
        self.assertEqual([name for name, _ in calls], ["web_search", "web_fetch"])
        self.assertEqual(calls[0][1]["session_id"], calls[1][1]["session_id"])
        self.assertEqual(calls[0][1]["search_queries"], ["贵州茅台 600519.SH 年度报告"])
        self.assertEqual(calls[1][1]["urls"], ["https://example.com/"])
        self.assertNotIn("model_name", calls[0][1])  # 此示例不调用模型。


if __name__ == "__main__":
    unittest.main()
