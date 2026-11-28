"""Local synthetic event feed for the declared stock factor example."""

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

EVENTS = {"600000.SH": (("2025-07-02", "2"), ("2025-07-03", "1.25")), "000001.SZ": ()}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        target = urlsplit(self.path)
        if target.path != "/events":
            self.send_error(404)
            return
        query = parse_qs(target.query)
        try:
            start = datetime.fromisoformat(query["start_time"][0]).date().isoformat()
            end = datetime.fromisoformat(query["end_time"][0]).date().isoformat()
            symbols = query["symbols"][0].split(",")
        except (ValueError, KeyError):
            self.send_error(400)
            return
        rows = []
        for symbol in symbols:
            if symbol in EVENTS:
                selected = [
                    {"ticker": symbol, "ex_date": day, "pre_over_post": ratio}
                    for day, ratio in EVENTS[symbol]
                    if start <= day <= end
                ]
                rows.extend(selected or [{"ticker": symbol, "status": "no_events"}])
        body = json.dumps({"data": {"events": rows}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=38053)
    with ThreadingHTTPServer(("127.0.0.1", parser.parse_args().port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/events", flush=True)
        server.serve_forever()
