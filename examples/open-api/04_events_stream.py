"""示例 4 — 事件流 SSE (任意有效 Token; 票据继承 scope)。

EventSource 带不了 Authorization 头, 走两步:
  POST /api/events/ticket (Bearer) → 一次性票据 (60 秒)
  GET  /api/events?ticket=…        → hello 帧 + 事件 (此处演示 alert 告警推送)

票据一次性: 断线重连需重新换票。浏览器端用 EventSource,
脚本端像本例一样逐行读流即可。
"""
import itertools

from common import call, sse

# 1. 换票 (任意 scope 的 Token 都可; 流里只推票据 scope 覆盖的事件)
ticket = call("POST", "/api/events/ticket")["ticket"]
print("票据已签发 (60s 内一次性)")

# 2. 订阅: hello 帧之后, 面板触发任意监控告警即可看到 alert 事件
for event in itertools.islice(sse(f"/api/events?ticket={ticket}"), 5):
    if event.get("event") == "hello":
        print("hello: scopes =", event.get("data", "").strip("{}"))
        continue
    print(f"[{event.get('event')}] {event.get('data', '')[:120]}")
print("(收到 5 帧后退出; 生产环境保持连接持续接收)")
