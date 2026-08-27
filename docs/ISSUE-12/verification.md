# 验证记录

```text
uv run --no-project python -m py_compile app/services/weak_to_strong.py app/api/research.py
# 通过

/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_weak_to_strong.py tests/api/test_research_api.py -q
# 21 passed in 2.06s
```

已验证：缺 reader 的结构化 unavailable、请求 schema/重复 symbol/交易词禁令，以及现有研究 API 回归。未验证：生产跨源 manifest、PIT 制度/ST/股本、真实分钟/竞价/逐笔/盘口 reader 与事件/OOS 主路径；这些按最终设计保持 unavailable。
