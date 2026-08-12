# TickFlow MVP v0.1.0

冻结状态：**功能闭环已冻结，策略未晋级，不可解释为生产 alpha。**

## 冻结结果

- 策略：趋势突破（`trend_breakout`）
- 数据区间：2024-04-01 至 2026-07-27
- 策略收益：-2.00%
- 上证基准：+7.89%
- 超额收益：-9.89%
- 最大回撤：-56.04%
- 交易数：386
- 协议哈希：`358a241014958743dbd862e3f2c60998939d6ff4e657db6dd2f84fd5a11e3425`

## 边界

- 包含：无前端 CLI、数据质量门、固定股票池、真实 Matrix 回测、JSON/HTML 报告。
- 不包含：盈利承诺、实盘交易、自动牛熊切换、AlphaGPT/PPO 调优。
- 当前工作区在冻结时仍有未提交改动，因此这是**产物与关键源码快照冻结**，不是 Git tag。

## 验证

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m scripts.freeze_mvp --verify-only
```

## 冻结文件

| 文件 | SHA-256 |
|---|---|
| `mvp_backtest.json` | `b3458edad226dc476087e8e6b6bfc67aa43960baa0950a3e541665782e24d05c` |
| `mvp_backtest.html` | `6eaf85742eac2aee0cfa9f1f3442afadbcdb860aa76e0258c11fbec7c6b69ce3` |
| `source/backend/scripts/run_mvp.py` | `0189d73e2ea5224a378ddb7fc52cfd1f674b2693ba79b61fe35cb5ebd1f55f23` |
| `source/backend/app/config.py` | `e77c26c171f626ab9432409496d96bbbd60706319f1463511b00d18236104a67` |
| `source/backend/app/main.py` | `3caa3cc0dd9bc79a291135e5dca1d33025d083454eca8d4daf19027c7efa0031` |
| `source/backend/app/backtest/engine.py` | `00e902af50e551d6e74fac7acd65f4ade469cd7243542613ceb392850f45f11b` |
| `source/backend/app/backtest/strategy.py` | `4d77affa221869c7f7db23a14380a81be69c695525ec488c1e961858df82f47c` |
| `source/backend/app/backtest/worker.py` | `50fc505cf5697c85e8b5e3c95080dd739f433e2b63a1eae25429caa9b0825340` |
| `source/backend/app/strategy/builtin/trend_breakout.py` | `b93df025af0246bb0b0f87bccd239362486ac5028a3f06e19060f0ab6c77cd2f` |
| `source/backend/research/common/universe.py` | `54eaa4da076f3309204d632a42b0057555c03d26e8189d61b598e22b256c536f` |
| `source/backend/research/paths.py` | `2f111f5fb01f6ed1a7d55c50b79d3d67e1a3858f99bb64a4355c6eb1681de60b` |
| `source/dev.ps1` | `feb433365a15886963abe023e880a5f017d8c69e174e256967b8294e6d3be0bf` |
| `source/dev.sh` | `f590b1a939cc87eb0411164ebc4a2deb94aeac0db8c30802478f1873e78f9d4e` |
| `source/.env.example` | `98146d71c5a77970060d88f189ad701652bcf2cb6c7b744cea34c42ccec91810` |
| `source/README.md` | `01a3cc63ac74afda76d91b438f149b50c3ef64ced2c327007f8442674b1e0abf` |
