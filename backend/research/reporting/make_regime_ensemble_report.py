"""生成 regime 干净归因(2x2) + 因子 ensemble + F4 深挖 的综合 HTML 报告。

读取:
  artifacts/current/strategy_regime_ensemble.json  (run_regime_ensemble.py 输出)
  artifacts/current/diag_f4_regime.json            (diag_f4_regime.py 输出)
写出:
  artifacts/current/regime_ensemble_report.html
"""
from __future__ import annotations

import json
from pathlib import Path

from research.paths import CURRENT_ARTIFACTS_DIR

ENS = CURRENT_ARTIFACTS_DIR / "strategy_regime_ensemble.json"
DIAG = CURRENT_ARTIFACTS_DIR / "diag_f4_regime.json"
OUT = CURRENT_ARTIFACTS_DIR / "regime_ensemble_report.html"

REGIME_MA60 = ["switch_leader", "flat_leader", "switch_ew", "flat_ew"]
REGIME_MA20 = ["switch_ew20", "flat_ew20", "mom_trend"]
ALL_CONFIGS = ["mom_trend", "flat_leader", "switch_leader", "flat_ew",
               "switch_ew", "flat_ew20", "switch_ew20", "ensemble"]
CONFIG_LABEL = {
    "mom_trend": "mom_trend (无regime)",
    "flat_leader": "flat_leader (牛mom/熊空仓·leader MA60)",
    "switch_leader": "switch_leader (牛mom/熊pullback·leader MA60)",
    "flat_ew": "flat_ew (牛mom/熊空仓·ew MA60)",
    "switch_ew": "switch_ew (牛mom/熊pullback·ew MA60)",
    "flat_ew20": "flat_ew20 (牛mom/熊空仓·ew MA20)",
    "switch_ew20": "switch_ew20 (牛mom/熊pullback·ew MA20)",
    "ensemble": "ensemble (6因子等权)",
}
COLOR = {
    "mom_trend": "#94a3b8",
    "flat_leader": "#2563eb",
    "switch_leader": "#7c3aed",
    "flat_ew": "#0ea5e9",
    "switch_ew": "#db2777",
    "flat_ew20": "#0891b2",
    "switch_ew20": "#ea580c",
    "ensemble": "#16a34a",
}


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def build_data(ens: dict, diag: dict) -> dict:
    folds = ens.get("folds", [])
    fold_ids = [f["fold"] for f in folds]

    def per_fold(key: str):
        out = []
        for f in folds:
            val = None
            for r in f.get("runs", []):
                if r.get("key") == key and "error" not in r:
                    val = round(float(r["total_return"]) * 100, 2)
                    break
            out.append(val)
        return out

    agg_rows = {}
    for k in ALL_CONFIGS:
        a = ens.get("aggregate", {}).get(k)
        if not a or a.get("mean_total_return") is None:
            agg_rows[k] = None
            continue
        agg_rows[k] = {
            "mean_ret": round(float(a["mean_total_return"]) * 100, 2),
            "mean_sharpe": round(float(a["mean_sharpe"]), 2),
            "mean_mdd": round(float(a["mean_max_drawdown"]) * 100, 2),
            "pos": a.get("positive_folds"),
            "nf": a.get("n_folds"),
        }

    f4_loss = None
    for f in folds:
        if f["fold"] == "F4":
            for r in f.get("runs", []):
                if r.get("key") in ("switch_ew", "switch_ew20") and "error" not in r:
                    f4_loss = round(float(r["total_return"]) * 100, 2)
                    break

    ew_by_month = diag.get("ew_signal", {}).get("by_month", {})
    ld_by_month = diag.get("leader_signal", {}).get("by_month", {})
    months = sorted(set(list(ew_by_month.keys()) + list(ld_by_month.keys())))

    data = {
        "fold_ids": fold_ids,
        "regime_ma60": {k: per_fold(k) for k in REGIME_MA60},
        "regime_ma20": {k: per_fold(k) for k in REGIME_MA20},
        "ens_per_fold": {"mom_trend": per_fold("mom_trend"),
                         "ensemble": per_fold("ensemble")},
        "agg_rows": agg_rows,
        "attribution": ens.get("attribution", {}),
        "diag": {
            "f4": diag.get("f4", {}),
            "index_path": diag.get("index_path", {}),
            "ew_signal": diag.get("ew_signal", {}),
            "leader_signal": diag.get("leader_signal", {}),
            "note": diag.get("note", ""),
            "months": months,
            "ew_by_month": [round(ew_by_month.get(m, {}).get("bull_pct", 0) * 100, 1) for m in months],
            "ld_by_month": [round(ld_by_month.get(m, {}).get("bull_pct", 0) * 100, 1) for m in months],
        },
        "f4_loss": f4_loss,
        "config_label": CONFIG_LABEL,
        "color": COLOR,
        "meta": ens.get("config", {}),
    }
    return data


def fmt_pct(x):
    return "—" if x is None else f"{x:+.2f}%"


def fmt_sharpe(x):
    return "—" if x is None else f"{x:+.2f}"


def attribution_table_html(attr: dict) -> str:
    rows = []
    names = {
        "action_effect_leader": "动作效应(leader MA60): switch − flat",
        "action_effect_ew": "动作效应(ew MA60): switch − flat",
        "signal_effect_flat": "信号效应(flat): leader − ew",
        "signal_effect_switch": "信号效应(switch): leader − ew",
        "action_effect_ew20": "动作效应(ew MA20, 干净): switch − flat",
    }
    for k, label in names.items():
        v = attr.get(k, {})
        if not v or v.get("mean_total_return") is None:
            rows.append(f"<tr><td>{label}</td><td colspan=3>无数据</td></tr>")
            continue
        dr = v.get("mean_total_return")
        ds = v.get("mean_sharpe")
        dm = v.get("mean_max_drawdown")
        rows.append(
            f"<tr><td>{label}</td>"
            f"<td class='{'pos' if (dr or 0)>=0 else 'neg'}'>{fmt_pct(round(dr*100,2))}</td>"
            f"<td class='{'pos' if (ds or 0)>=0 else 'neg'}'>{fmt_sharpe(round(ds,2))}</td>"
            f"<td class='{'pos' if (dm or 0)>=0 else 'neg'}'>{fmt_pct(round(dm*100,2))}</td></tr>"
        )
    return "\n".join(rows)


def main():
    ens = load(ENS)
    diag = load(DIAG) if DIAG.exists() else {}
    data = build_data(ens, diag)
    attr_html = attribution_table_html(data["attribution"])

    fold_ids = data["fold_ids"]
    table_rows = []
    for k in ALL_CONFIGS:
        if k in REGIME_MA60:
            pf = data["regime_ma60"].get(k, [])
        elif k in REGIME_MA20:
            pf = data["regime_ma20"].get(k, [])
        else:
            pf = data["ens_per_fold"].get(k, [])
        if not pf:
            pf = [None] * len(fold_ids)
        cells = "".join(f"<td class='{'pos' if (c or 0)>=0 else 'neg'}'>{fmt_pct(c)}</td>"
                        for c in pf)
        ar = data["agg_rows"].get(k)
        if ar:
            mean = (f"<td class='pos'>{fmt_pct(ar['mean_ret'])}</td>"
                    f"<td class='pos'>{fmt_sharpe(ar['mean_sharpe'])}</td>"
                    f"<td class='neg'>{fmt_pct(ar['mean_mdd'])}</td>"
                    f"<td>{ar['pos']}/{ar['nf']}</td>")
        else:
            mean = "<td colspan=4>无数据/缺失</td>"
        table_rows.append(
            f"<tr><td><b>{data['config_label'][k]}</b></td>{cells}{mean}</tr>"
        )
    table_html = "\n".join(table_rows)

    d = data["diag"]
    ip = d["index_path"]
    ew = d["ew_signal"]
    ld = d["leader_signal"]
    f4 = d["f4"]
    note = d["note"]
    f4_loss = data["f4_loss"]
    f4_loss_txt = (f"本轮 switch_ew / switch_ew20 在 F4 实测亏损 <b>{fmt_pct(f4_loss)}</b>。"
                   if f4_loss is not None else "本轮 F4 switch 结果缺失。")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>regime 归因 + 因子 ensemble + F4 深挖</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{ --bg:#f7f8fa; --card:#fff; --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --pos:#16a34a; --neg:#dc2626; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.6; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  h2 {{ font-size:20px; margin:34px 0 10px; padding-left:10px; border-left:4px solid #2563eb; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:6px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:18px 20px; margin:14px 0; box-shadow:0 1px 3px rgba(0,0,0,.04); }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  @media(max-width:760px){{ .grid2{{grid-template-columns:1fr;}} }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ border:1px solid var(--line); padding:6px 8px; text-align:center; }}
  th {{ background:#f1f5f9; }}
  td.pos,.pos {{ color:var(--pos); }}
  td.neg,.neg {{ color:var(--neg); }}
  .note {{ background:#fef9ec; border:1px solid #fde68a; border-radius:10px; padding:14px 16px; font-size:14px; }}
  .verdict {{ background:#eef2ff; border:1px solid #c7d2fe; border-radius:10px; padding:14px 16px; }}
  .tag {{ display:inline-block; background:#e0e7ff; color:#3730a3; border-radius:6px;
         padding:1px 8px; font-size:12px; margin-right:6px; }}
  canvas {{ max-height:320px; }}
  .kv {{ font-size:14px; }}
  .kv b {{ color:#111827; }}
  .small {{ font-size:12px; color:var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>regime 条件化 · 归因 + 因子 ensemble + F4 深挖</h1>
  <div class="sub">tickflow 量化项目 · 历史 walk-forward 复验（4 折，同一次运行内 8 配置可控对比；非 fresh OOS）</div>

  <div class="note">
    <b>方法学口径：</b>本报告 8 个配置在<b>同一次运行</b>内共享 400 只 universe（seed={data['meta'].get('seed','20260723')}）与 4 折切分，
    故彼此<b>可直接对比</b>。本轮已修复旧脚本的非确定性抽样：先按 symbol 排序再以 seed 采样，
    universe SHA-256=<code>{data['meta'].get('universe_sha256','missing')}</code>。
    修复前的报告无法证明同 seed 对应同一 universe，已降级为 legacy，不能继续作为权威收益结论。
    F1–F4 已参与过早期因子/策略选择，本轮只验证 canonical universe 下的相对结果，不构成新的封存测试。
  </div>

  <h2>① regime 干净归因（2×2：信号源 × 熊市动作）</h2>
  <div class="sub"><b>信号 bug 已修复：</b>复刻引擎 leader 信号时手写累计和 MA 漏减窗口首项 → 全判熊（0.2% 牛）；
  改 polars <code>rolling_mean</code> 后 leader 龙头指数实际 <b>~70% 牛市</b>（涨 ~10%）。
  <b>真实广度背离：</b>等权 400 只篮子在本区间持续阴跌 → ew(MA60/MA20) 信号<b>仍 0% 牛</b>（非 bug，是样本属性）。
  故 leader 轴 2×2 现在<b>有意义</b>（牛腿真部署），ew 轴作为「广度背离」对照保留。</div>

  <div class="grid2">
    <div class="card"><canvas id="cRegime"></canvas>
      <div class="small">leader MA60（已修复，~70%牛）：switch/flat 可干净对比动作效应</div></div>
    <div class="card"><canvas id="cMa20"></canvas>
      <div class="small">ew MA20（仍 0%牛，广度背离）：纯 pullback 腿，作对照</div></div>
  </div>
  <div class="card">
    <div class="sub" style="margin-bottom:8px;">归因差值（switch−flat / leader−ew，正=切换加值）</div>
    <table>
      <thead><tr><th>对比</th><th>Δ收益</th><th>ΔSharpe</th><th>ΔMDD</th></tr></thead>
      <tbody>{attr_html}</tbody>
    </table>
  </div>

  <h2>② 因子 ensemble（6 语义动量因子横截面归一等权）</h2>
  <div class="sub">ensemble = mom_trend / mom_vol / mom_anti_ext / mom_rsi / ma20_dev / mom20 六因子逐股 z-score 平均。</div>
  <div class="card"><canvas id="cEns"></canvas></div>
  <div class="card">
    <div class="sub" style="margin-bottom:8px;">所有配置跨区间均值（同运行内对比）</div>
    <table>
      <thead><tr><th>配置</th>
        <th>F1</th><th>F2</th><th>F3</th><th>F4</th>
        <th>均值收益</th><th>均值Sharpe</th><th>均值MDD</th><th>正折</th></tr></thead>
      <tbody>{table_html}</tbody>
    </table>
  </div>
  <div class="grid2">
    <div class="card"><canvas id="cRet"></canvas></div>
    <div class="card"><canvas id="cSharpe"></canvas></div>
  </div>
  <div class="card"><canvas id="cMdd"></canvas></div>

  <h2>③ F4 深挖：regime_switch 炸裂根因</h2>
  <div class="card kv">
    <div><span class="tag">区间</span> {f4.get('start','?')} ~ {f4.get('end','?')}</div>
    <div><span class="tag">等权指数</span> 实测 <b>+{ip.get('idx_net_pct','?')}%</b>，
         但全程位于 MA60 下方（均值|偏离| {ip.get('idx_mean_abs_dev_from_ma_pct','?')}%，
         最大 {ip.get('idx_max_dev_from_ma_pct','?')}%）</div>
    <div><span class="tag">ew(MA60) 信号</span> 牛市 {ew.get('bull_pct',0)*100:.0f}% / 熊市 {ew.get('bear_pct',0)*100:.0f}%，
         翻转 <b>{ew.get('regime_flips',0)}</b> 次，平均持仓 {ew.get('avg_run_len','?')} 天</div>
    <div><span class="tag">leader(MA60) 信号</span> 牛市 {ld.get('bull_pct',0)*100:.0f}% / 熊市 {ld.get('bear_pct',0)*100:.0f}%，
         翻转 <b>{ld.get('regime_flips',0)}</b> 次</div>
    <div style="margin-top:6px;">{f4_loss_txt}</div>
  </div>
  <div class="card"><canvas id="cF4"></canvas>
    <div class="small">ew(MA60) 信号在 F4 全程 0% 牛市（卡错腿 / 广度背离）；leader 信号 ~70% 牛更灵敏。</div>
  </div>
  <div class="note">{note}</div>

  <h2>诚实结论</h2>
  <div class="verdict" id="verdict"></div>

  <div class="card sub">
    产物：regime_ensemble_report.html（本报告）· strategy_regime_ensemble.json（8 配置 4 折结果+归因）·
    diag_f4_regime.json（F4 信号诊断）。
  </div>
</div>

<script>
const DATA = {json.dumps(data, ensure_ascii=False)};
const C = DATA.color, L = DATA.config_label;
function mk(ctx, cfg){{ new Chart(ctx, cfg); }}
const gridC = "#e5e7eb";
function gset(obj) {{ return Object.keys(obj).map(k=>({{label:L[k], data:obj[k], backgroundColor:C[k], borderRadius:4}})); }}

// ① MA60 退化
mk(document.getElementById('cRegime'), {{
  type:'bar',
  data:{{ labels:DATA.fold_ids, datasets:gset(DATA.regime_ma60) }},
  options:{{ responsive:true, plugins:{{legend:{{position:'top'}}}},
    scales:{{ y:{{grid:{{color:gridC}}, title:{{display:true,text:'OOS 收益 %'}}}}, x:{{grid:{{display:false}}}} }} }}
}});
// ① MA20 干净
mk(document.getElementById('cMa20'), {{
  type:'bar',
  data:{{ labels:DATA.fold_ids, datasets:gset(DATA.regime_ma20) }},
  options:{{ responsive:true, plugins:{{legend:{{position:'top'}}}},
    scales:{{ y:{{grid:{{color:gridC}}, title:{{display:true,text:'OOS 收益 %'}}}}, x:{{grid:{{display:false}}}} }} }}
}});
// ② ensemble
mk(document.getElementById('cEns'), {{
  type:'bar',
  data:{{ labels:DATA.fold_ids, datasets:[
    {{label:L['mom_trend'], data:DATA.ens_per_fold.mom_trend, backgroundColor:C['mom_trend'], borderRadius:4}},
    {{label:L['ensemble'], data:DATA.ens_per_fold.ensemble, backgroundColor:C['ensemble'], borderRadius:4}} ] }},
  options:{{ responsive:true, plugins:{{legend:{{position:'top'}}}},
    scales:{{ y:{{grid:{{color:gridC}}, title:{{display:true,text:'OOS 收益 %'}}}}, x:{{grid:{{display:false}}}} }} }}
}});
// ③ 均值
function hbar(id, field, title) {{
  const keys = Object.keys(DATA.agg_rows).filter(k=>DATA.agg_rows[k]);
  const vals = keys.map(k=>DATA.agg_rows[k][field]);
  mk(document.getElementById(id), {{
    type:'bar', indexAxis:'y',
    data:{{ labels:keys.map(k=>L[k]), datasets:[{{data:vals, backgroundColor:keys.map(k=>C[k]), borderRadius:4}}] }},
    options:{{ responsive:true, plugins:{{legend:{{display:false}}}},
      scales:{{ x:{{grid:{{color:gridC}}, title:{{display:true,text:title}}}}, y:{{grid:{{display:false}}}} }} }}
  }});
}}
hbar('cRet','mean_ret','均值收益 %');
hbar('cSharpe','mean_sharpe','均值 Sharpe');
hbar('cMdd','mean_mdd','均值 MDD % (越负越差)');
// ④ F4
mk(document.getElementById('cF4'), {{
  type:'bar',
  data:{{ labels:DATA.diag.months, datasets:[
    {{label:'ew(MA60) 牛市%', data:DATA.diag.ew_by_month, backgroundColor:'#db2777', borderRadius:4}},
    {{label:'leader(MA60) 牛市%', data:DATA.diag.ld_by_month, backgroundColor:'#2563eb', borderRadius:4}} ] }},
  options:{{ responsive:true, plugins:{{legend:{{position:'top'}}}},
    scales:{{ y:{{grid:{{color:gridC}}, max:100, title:{{display:true,text:'牛市占比 %'}}}}, x:{{grid:{{display:false}}}} }} }}
}});

// 结论
(function(){{
  const a = DATA.attribution; const ar = DATA.agg_rows;
  const f = (x)=> x==null?'—':(x>=0?'+':'')+x.toFixed(2);
  const parts = [];
  const ae = a.action_effect_ew20;
  if (ae && ae.mean_total_return!=null)
    parts.push(`动作效应(ew MA20): switch−flat 收益 ${{f(ae.mean_total_return*100)}}%、Sharpe ${{f(ae.mean_sharpe)}}、MDD ${{f(ae.mean_max_drawdown*100)}}%`);
  const aeL = a.action_effect_leader;
  if (aeL && aeL.mean_total_return!=null)
    parts.push(`动作效应(leader MA60): switch−flat 收益 ${{f(aeL.mean_total_return*100)}}%（正=熊市切 pullback 优于空仓；负=空仓更稳）`);
  const se = a.signal_effect_switch;
  if (se && se.mean_total_return!=null)
    parts.push(`信号效应(switch): leader−ew 收益 ${{f(se.mean_total_return*100)}}%（正=leader 信号更优）`);
  if (ar.ensemble && ar.mom_trend)
    parts.push(`因子 ensemble 均值收益 ${{f(ar.ensemble.mean_ret)}}% vs mom_trend ${{f(ar.mom_trend.mean_ret)}}%，Sharpe ${{f(ar.ensemble.mean_sharpe)}} vs ${{f(ar.mom_trend.mean_sharpe)}}（6 因子同属动量族，等权平均分散收益有限）`);
  parts.push(`F4 根因：ew MA60 信号滞后+广度背离 → 整段卡在熊腿(pullback)逆上涨市；换更灵敏/更短信号或给熊腿加趋势确认可修。`);
  document.getElementById('verdict').innerHTML =
    '<b>0. 旧 flat_leader“最强”结论已撤回。</b> 修复 universe 确定性后，flat_leader 四折均值为 '+
    `${{f(ar.flat_leader.mean_ret)}}%、正收益 ${{ar.flat_leader.pos}}/${{ar.flat_leader.nf}} 折；当前没有足够证据晋级任何策略。\n`+
    '<b>1. regime 信号 bug 已修复，2×2 现在有意义。</b> 修前 leader 信号被手写累计和 MA 写崩（全判熊 0.2%），'+
    '导致 switch_leader≡switch_ew 假象；改用 rolling_mean 后 leader 龙头指数实际 ~70% 牛市，'+
    'switch_leader 与 switch_ew 现在<b>真不同</b>（leader 部署 mom 牛腿、ew 卡在 pullback 熊腿）。\\n'+
    '<b>2. 真实广度背离：龙头涨 ~10%、等权篮子阴跌。</b> ew(MA60/MA20) 全程 0% 牛是本 universe 等权篮子的<b>样本属性</b>非 bug，'+
    '故 leader 轴可作干净动作归因，ew 轴作「广度背离」对照。\\n'+
    '<b>3. 因子 ensemble 是互补不是银弹。</b> 6 因子高度共线（同为动量族），等权平均未显著优于 mom_trend——'+
    '下一步应做因子正交 / IC 加权而非简单平均。\\n'+
    '<b>4. F4 炸裂是「信号滞后卡错腿」，非「过度切换」。</b>\\n\\n'+
    parts.map(p=>`· ${{p}}`).join('<br>');
}})();
</script>
</body>
</html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"report -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
