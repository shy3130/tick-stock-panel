# Issue #16：MACD(10/20/7) 阶段研究

本目录冻结 MACD 10/20/7、逐日状态机、PIT/generation/raw 字段、T+1 和 OOS 契约，并记录当前能力缺失。

## 阅读顺序

1. [feasibility.md](feasibility.md)：代码事实、可行性与缺口矩阵；
2. [plan-v1.md](plan-v1.md)：初版方案；
3. [review-v1.md](review-v1.md)：初版否决意见；
4. [plan-v2.md](plan-v2.md)：修订方案；
5. [review-v2.md](review-v2.md)：修订方案评审；
6. [final-design.md](final-design.md)：最终字段、状态机、T+1、OOS 与 API 契约；
7. [verification.md](verification.md)：编译、定向测试和范围检查记录。

## 当前交付

- `backend/app/services/macd_stages.py`：纯函数能力声明；
- `GET /api/research/macd-stages`：HTTP 200 + `status="unavailable"`；
- `backend/tests/test_macd_stages.py`：固定参数、原因集合、确定性和无伪造序列测试。

逐日状态机、OOS 执行器和 PIT 读取器尚未实现；因此即使基础行情能力恢复，端点也不会返回阶段序列。
