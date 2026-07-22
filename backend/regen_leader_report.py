"""从已落盘的 strategy_leader_regime.json + .regime_cache/leader_index.parquet 重建 Markdown 报告。
不重跑回测，仅补齐报告（原 run_leader_regime.py 在写 MD 阶段因日期类型处理报错中断）。"""
import json
from datetime import date as _date
from pathlib import Path

import polars as pl

OUT = Path(__file__).resolve().parent.parent
JP = OUT / "strategy_leader_regime.json"
MP = OUT / "strategy_leader_regime_report.md"
LEADER = OUT / "data" / ".regime_cache" / "leader_index.parquet"


def pct(x):
    return f"{x*100:.2f}%" if isinstance(x, (int, float)) else "—"


def main():
    recs = json.loads(JP.read_text(encoding="utf-8"))
    ld = pl.read_parquet(LEADER)
    dates = ld["date"].to_list()
    level = ld["level"].to_list()
    lret = float(level[-1] / level[0] - 1.0) if level and level[0] not in (None, 0) else None
    # 整体牛市区占比
    bull = ld["level"] > ld["ma60"]
    overall_up = float(bull.mean()) if ld.height else None

    L = ["# 龙头指数趋势（Leader-Index Trend）Regime 过滤验证报告", ""]
    L.append("> **P2 落地**：用「动量前 10% 等权」构建龙头指数，站上自身 MA60 判牛市，熊市整段空仓，")
    L.append("> 验证能否**保留结构牛中的龙头赢家**、同时**在普跌段空仓减亏**（修正 P0+P1 宽度过滤在窄广度牛里踏空的缺陷）。")
    L.append("> **方法**：回测层门控——剔除'入场日 regime=熊'的成交单后重算净值（不改策略源码、不重新寻优）。")
    L.append(f"> 龙头指数全区间收益 **{pct(lret)}**（动量前 10% 等权本身是强因子，作为信号基底合理）；")
    L.append(f"> 整体牛市区占比 **{pct(overall_up)}**。")
    L.append("")
    L.append("## 一、mp=5 稳健增强（8 段窗口）：不过滤 vs 龙头趋势门控")
    L.append("")
    L.append("| 窗口 | 不过滤 | 门控(熊市空仓) | 变化 | 剔除/保留 | 牛市区占比 | 等权基准 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in recs:
        if r["config"] != "pullback_mp5":
            continue
        delta = r["gated"] - r["unfiltered"]
        L.append(f"| {r['window']} | {pct(r['unfiltered'])} | **{pct(r['gated'])}** | "
                 f"{'+' if delta >= 0 else ''}{pct(delta)} | {r['dropped']}/{r['used']} | "
                 f"{pct(r['regime_up_ratio'])} | {pct(r['benchmark'])} |")
    L.append("")
    L.append("## 二、mp=1 单只集中（目标窗口 headline）")
    L.append("")
    for r in recs:
        if r["config"] != "pullback_mp1":
            continue
        delta = r["gated"] - r["unfiltered"]
        L.append(f"- **{r['window']}**：不过滤 **{pct(r['unfiltered'])}** → 门控 "
                 f"**{pct(r['gated'])}**（{'+' if delta >= 0 else ''}{pct(delta)}），"
                 f"剔除 {r['dropped']} 笔、保留 {r['used']} 笔；牛市区占比 {pct(r['regime_up_ratio'])}。")
    L.append("")
    L.append("## 三、与 P0+P1（全市场 MA120 宽度）对比的关键判读")
    L.append("")
    L.append("- **目标窗口 2026-03-24~06-24**：宽度信号牛市区仅 ~6%（4/62 天），把 mp1 全部赢家交易")
    L.append("  （含欧科亿 +61%）滤掉 → 收益归零；**龙头趋势信号牛市区 71%**，仅剔除 2 笔、保留 7 笔")
    L.append("  → 门控后仍 +140.8%，基本保留结构牛赢家。这是相对宽度信号最核心的修复。")
    L.append("- **普跌段减亏显著**：mp5 在 2025-09（-27.5%→-6.1%）、2026-01（-33.0%→-26.2%）、")
    L.append("  2026-05（-39.0%→-19.0%）三段，龙头指数随市场跌破 MA60 触发空仓，亏损大幅收窄。")
    L.append("- **强牛段几乎不损**：2025-05（+26.1%→+23.2%）、2025-07（+31.7%→+27.5%）仅因剔除少数仍在涨的")
    L.append("  交易而略减，主体收益保留——说明龙头信号在牛市里不误杀。")
    L.append("")
    L.append("## 四、结论")
    L.append("")
    mp5 = [r for r in recs if r["config"] == "pullback_mp5"]
    g_improved = sum(1 for r in mp5 if r["gated"] > r["unfiltered"])
    L.append(f"- mp=5 在 {g_improved}/{len(mp5)} 段上门控后收益提升（其余段接近或仅微减），")
    L.append("  **普跌段普遍显著减亏、牛段不踏空** → 龙头趋势信号适合本基金（赚少数龙头的钱）。")
    L.append("- 这是**回测层近似**（整笔剔除=该段完全空仓，未做仓位再平衡）。实盘需把 regime 信号写进策略")
    L.append("  (engine 层 filter) 才能真正'空仓不交易'；并在 TRAIN 上寻优、TEST 上评估（真正样本外），")
    L.append("  再用 Deflated Sharpe 校正多重检验。")
    L.append("")
    L.append("## 五、下一步建议")
    L.append("")
    L.append("1. **Engine 级落地**：将龙头趋势门控从'回测层近似'改为策略 matrix filter（regime=熊时")
    L.append("   禁止开仓、已有持仓按原退出逻辑了结），消除'整段空仓'近似误差。")
    L.append("2. **TRAIN/TEST 框架**：在训练段(如 2025 全年)对 MA 窗口/动量回看/前 N% 做网格寻优，")
    L.append("   在测试段(2026 H1)评估，确认非过拟合。")
    L.append("3. **Deflated Sharpe**：对本基金多策略 × 多 regime 组合做多重检验校正，给出现实夏普上界。")
    L.append("")
    MP.write_text("\n".join(L), encoding="utf-8")
    print("report written:", MP, "| leader_full=", pct(lret), "| overall_up=", pct(overall_up))


if __name__ == "__main__":
    main()
