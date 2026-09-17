"""主程序：按《安全吃股息 V3.0》系统扫描股票池并生成静态 HTML 报告。

用法：
    python3 screener.py              # 全量扫描
    python3 screener.py --limit 5    # 只扫前 5 只（调试）
    python3 screener.py --clear      # 清空缓存后扫描
    python3 screener.py --json       # 额外输出 result.json

输出：
    docs/index.html    手机端可读的静态报告（无 JavaScript，微信浏览器可直接打开）
    docs/data.json     结构化结果，供二次分析
    history.json       本次结果快照，用于「NEW」标记对比
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

import data_source as ds  # noqa: E402
import strategy as st  # noqa: E402
from pool import DPS_MODE_LABEL, LEVEL_LABEL, SECTOR_LIMITS, pool_df  # noqa: E402

OUT_DIR = Path("docs")
HISTORY = Path("history.json")

STATE_STYLE = {
    "heavy": ("重仓档", "s-heavy"),
    "add": ("加仓档", "s-add"),
    "entry": ("首仓档", "s-entry"),
    "near": ("接近买点", "s-near"),
    "watch": ("观察", "s-watch"),
}
LEVEL_STYLE = {
    "core": ("核心", "lv-core"),
    "standard": ("标准", "lv-std"),
    "watch": ("观察", "lv-watch"),
    "special": ("特殊", "lv-special"),
    "standalone": ("独立", "lv-standalone"),
}


# ---------------------------------------------------------------- 扫描
def scan(limit=None, use_cache=True):
    pool = pool_df()
    if limit:
        pool = pool.head(limit)

    codes = pool["code"].tolist()
    print(f"[1/4] 拉取行情 {len(codes)} 只 ...")
    quotes = ds.fetch_quotes(codes)
    print(f"      成功 {len(quotes)} 只")

    rows = []
    total = len(pool)
    for i, r in pool.iterrows():
        code = r["code"]
        print(f"[2/4] ({i+1}/{total}) {r['name']} {code} 分红/财务 ...", end="\r")
        div_years = ds.fetch_dividend_years(code)
        fin = ds.fetch_financials(code)
        rec = st.evaluate(
            code=code,
            name=r["name"],
            cat=r["cat"],
            sub=r["sub"],
            mode=r["dps_mode"],
            entry=r["entry"],
            add=r["add"],
            heavy=r["heavy"],
            level=r["level"],
            cap=r["cap"],
            need_cycle=bool(r["need_cycle"]),
            quote=quotes.get(code),
            div_years=div_years,
            fin=fin,
            in_book=bool(r.get("in_book", True)),
        )
        rows.append(rec)
    print(" " * 70, end="\r")

    df = pd.DataFrame(rows)

    # ---- NEW 标记
    prev = load_previous()
    prev_entry = set(prev.get("entry_codes", []))
    df["is_new"] = df["code"].apply(
        lambda c: (c not in prev_entry) if prev_entry else False
    )

    save_history(df)
    return df


def load_previous():
    if not HISTORY.exists():
        return {}
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(df):
    entry_codes = df[df["state"].isin(["entry", "add", "heavy"])]["code"].tolist()
    data = {
        "updated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "entry_codes": sorted(entry_codes),
        "all_codes": sorted(df["code"].tolist()),
    }
    HISTORY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      已保存快照：可买 {len(entry_codes)} 只 / 全池 {len(df)} 只")


# ---------------------------------------------------------------- 渲染
def esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def money(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}"


def pct(v, nd=2, sign=False):
    if v is None:
        return "-"
    s = f"{v * 100:.{nd}f}%"
    if sign and v > 0:
        s = "+" + s
    return s


def build_row(r, idx):
    st_key = r["state"] if r["state"] in STATE_STYLE else "watch"
    st_label, st_cls = STATE_STYLE[st_key]
    lv_label, lv_cls = LEVEL_STYLE.get(r["level"], ("-", "lv-std"))
    chg_cls = "up" if r["change_pct"] > 0 else ("down" if r["change_pct"] < 0 else "flat")

    new_tag = '<span class="tag-new">NEW</span>' if r.get("is_new") else ""
    ext_tag = "" if r.get("in_book", True) else '<span class="tag-ext">扩展</span>'

    # 分红趋势微标
    risk_map = {"ok": "", "caution": '<span class="tag-warn">留意</span>',
                "risk": '<span class="tag-risk">风险</span>'}
    risk_tag = risk_map.get(r["risk_level"], "")

    flag_html = ""
    if r["flags"]:
        flag_html = '<div class="flag">' + esc("；".join(r["flags"])) + "</div>"

    note_html = ""
    if r["notes"]:
        note_html = '<div class="note">' + esc(" · ".join(r["notes"])) + "</div>"
    if r["dps_note"]:
        note_html += '<div class="note">' + esc(r["dps_note"]) + "</div>"

    gap_txt = "-"
    gap_cls = "flat"
    if r["gap"] is not None and pd.notna(r["gap"]):
        if r["gap"] <= 0:
            gap_txt = f"已到价 {pct(abs(r['gap']), 1)}"
            gap_cls = "down"
        else:
            gap_txt = f"还需跌 {pct(r['gap'], 1)}"
            gap_cls = "up"

    # 首仓 / 加仓 / 重仓 价格（特殊资产无固定三档）
    if r["state"] == "none" or r["pricing_dps"] is None or pd.isna(r["pricing_dps"]):
        tp = '<span class="na">不设固定档</span>'
    else:
        tp = (
            '<span class="tp">' + money(r["entry_price"]) + "</span>"
            + '<span class="sep">/</span>'
            + '<span class="tp">' + money(r["add_price"]) + "</span>"
            + '<span class="sep">/</span>'
            + '<span class="tp">' + money(r["heavy_price"]) + "</span>"
        )

    # 52周分位
    pos_txt = "-"
    if r["high52"] > r["low52"] > 0:
        pos_txt = pct((r["price"] - r["low52"]) / (r["high52"] - r["low52"]), 0)

    depth_txt = pct(r["depth"], 1, True) if pd.notna(r["depth"]) else "-"

    return f"""<tr class="{st_cls}">
<td class="c-idx">{idx}</td>
<td class="c-name">
  <div class="nm">{esc(r['name'])}{new_tag}{ext_tag}{risk_tag}</div>
  <div class="cd">{ds.full_code(r['code'])} · <span class="{lv_cls}">{lv_label}</span></div>
  <div class="sub">{esc(r['cat'])} · {esc(r['sub'])}</div>
  {flag_html}{note_html}
</td>
<td class="c-price">
  <div class="px">{money(r['price'])}</div>
  <div class="{chg_cls}">{pct(r['change_pct']/100, 2, True)}</div>
</td>
<td class="c-yield">
  <div class="yd">{pct(r['pricing_yield'], 2)}</div>
  <div class="yds">静态 {pct(r['static_yield'], 2)}</div>
</td>
<td class="c-dps">
  <div class="dps">{money(r['pricing_dps'], 3)}</div>
  <div class="dpsb">{esc(r['dps_basis'].split('（')[0])}</div>
</td>
<td class="c-band">
  <span class="band {st_cls}">{st_label}</span>
  <div class="depth">{depth_txt}</div>
</td>
<td class="c-tp">{tp}
  <div class="thr">{('门槛 ' + pct(r['entry_y'],1) + '/' + pct(r['add_y'],1) + '/' + pct(r['heavy_y'],1)) if r['entry_y'] > 0 else '按周期评分与净现金判定'}</div>
</td>
<td class="c-gap"><span class="{gap_cls}">{gap_txt}</span></td>
<td class="c-pos">
  <div class="pctl">{pos_txt}</div>
  <div class="ph">高 {money(r['high52'])}</div>
  <div class="pl">低 {money(r['low52'])}</div>
</td>
<td class="c-pe">{money(r['pe'], 1)}</td>
<td class="c-pb">{money(r['pb'], 2)}</td>
<td class="c-cap">{pct(r['cap'], 0)}</td>
</tr>"""


def render(df, out_path: Path):
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    total = len(df)
    buy = df[df["state"].isin(["entry", "add", "heavy"])]
    near = df[df["state"] == "near"]
    watch = df[df["state"] == "watch"]
    vetoed = df[df["veto"]]
    new_cnt = int(df["is_new"].sum()) if "is_new" in df else 0

    # 按状态排序：重仓 > 加仓 > 首仓 > 接近 > 观察；同级按估值深度排
    order = {"heavy": 0, "add": 1, "entry": 2, "near": 3, "watch": 4}
    df2 = df.copy()
    df2["_o"] = df2["state"].map(lambda s: order.get(s, 9))
    df2["_d"] = df2["depth"].fillna(-9)
    df2 = df2.sort_values(["_o", "_d"], ascending=[True, False])

    rows_html = "\n".join(
        build_row(r, i + 1) for i, (_, r) in enumerate(df2.iterrows())
    )

    # 大类统计
    sec_rows = []
    for cat, grp in df.groupby("cat"):
        cnt = len(grp)
        can = len(grp[grp["state"].isin(["entry", "add", "heavy"])])
        lo, mid, hi = SECTOR_LIMITS.get(cat, (0, 0, 0))
        sec_rows.append(
            f'<tr><td>{esc(cat)}</td><td>{cnt}</td><td>{can}</td>'
            f"<td>{pct(lo,0)}-{pct(mid,0)}</td><td>上限 {pct(hi,0)}</td></tr>"
        )
    sec_html = "\n".join(sec_rows)

    # 假高股息警示名单
    flagged = [
        (r["name"], r["cat"], r["pricing_yield"], r["flags"])
        for _, r in df.iterrows()
        if isinstance(r.get("flags"), list) and len(r["flags"]) > 0
    ]
    flag_box = ""
    if flagged:
        items = "".join(
            f'<li><b>{esc(n)}</b>（{esc(c)}，定价股息率 {pct(y,2)}）：{esc("；".join(fs))}</li>'
            for n, c, y, fs in sorted(flagged, key=lambda x: -(x[2] or 0))[:12]
        )
        flag_box = f"""
  <div class="box warn">
    <h3>假高股息警示（第 23 章）</h3>
    股息率越高，越要问「为什么这么高」。以下标的命中了假高股息识别信号，
    其档位已自动降级，买入前必须人工复核未来 DPS：
    <ul>{items}</ul>
  </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>安全吃股息 · V3 策略扫描</title>
<style>
:root{{
  --bg:#f4f5f7; --card:#fff; --text:#16181d; --sub:#6b7280; --muted:#9ca3af;
  --line:#e5e7eb; --red:#d92b2b; --green:#0f9d58; --blue:#2563eb;
  --gold:#b45309; --gold-bg:#fff7e6; --gold-line:#fcd34d;
  --green-bg:#f0fdf4; --green-line:#86efac;
  --blue-bg:#eff6ff; --blue-line:#93c5fd;
  --grey-bg:#fafafa;
}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}}
html,body{{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px;-webkit-text-size-adjust:100%;}}
.wrap{{padding:12px;max-width:1600px;margin:0 auto;}}
h1{{font-size:17px;margin:0 0 4px;font-weight:700;}}
.sub-title{{font-size:11px;color:var(--sub);margin-bottom:12px;line-height:1.6;}}
.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;}}
.card .k{{font-size:11px;color:var(--sub);}}
.card .v{{font-size:20px;font-weight:700;margin-top:2px;}}
.card .v.red{{color:var(--red);}}
.card .v.gold{{color:var(--gold);}}
.card .v.green{{color:var(--green);}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px;font-size:12px;line-height:1.75;color:#374151;}}
.box b{{color:var(--text);}}
.box h3{{font-size:13px;margin:0 0 6px;}}
.legend{{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 0;font-size:11px;}}
.legend span{{padding:2px 8px;border-radius:10px;border:1px solid var(--line);background:#fff;}}
.tw{{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto;-webkit-overflow-scrolling:touch;}}
table{{border-collapse:separate;border-spacing:0;width:100%;min-width:1080px;font-size:12px;}}
thead th{{background:#f9fafb;color:var(--sub);font-weight:600;padding:8px 6px;text-align:center;border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0;z-index:10;font-size:11px;}}
tbody td{{padding:9px 6px;text-align:center;vertical-align:top;border-bottom:1px solid var(--line);}}
tbody tr.s-heavy{{background:var(--gold-bg);}}
tbody tr.s-add{{background:var(--green-bg);}}
tbody tr.s-entry{{background:var(--blue-bg);}}
tbody tr.s-near{{background:#fefce8;}}
tbody tr.s-watch{{background:var(--grey-bg);}}
.c-idx{{width:28px;color:var(--muted);font-size:11px;}}
.c-name{{min-width:150px;max-width:190px;text-align:left;}}
.nm{{font-weight:700;font-size:13px;line-height:1.4;}}
.cd{{font-size:10px;color:var(--muted);margin-top:1px;}}
.sub{{font-size:10px;color:#8b8f96;margin-top:1px;line-height:1.4;}}
.flag{{font-size:10px;color:#b45309;background:#fff7e6;border:1px solid #fcd34d;border-radius:4px;padding:2px 4px;margin-top:3px;line-height:1.4;text-align:left;}}
.note{{font-size:10px;color:#9ca3af;margin-top:2px;line-height:1.4;text-align:left;}}
.c-price{{min-width:60px;}}
.px{{font-weight:700;font-size:14px;}}
.up{{color:var(--red);font-size:11px;font-weight:600;}}
.down{{color:var(--green);font-size:11px;font-weight:600;}}
.flat{{color:var(--muted);font-size:11px;}}
.c-yield{{min-width:64px;}}
.yd{{font-weight:700;font-size:15px;color:var(--red);}}
.yds{{font-size:10px;color:var(--muted);margin-top:2px;}}
.c-dps{{min-width:62px;}}
.dps{{font-weight:600;font-size:13px;}}
.dpsb{{font-size:9px;color:var(--muted);margin-top:2px;line-height:1.3;}}
.c-band{{min-width:82px;}}
.band{{display:inline-block;padding:3px 9px;border-radius:11px;font-size:11px;font-weight:700;white-space:nowrap;}}
.band.s-heavy{{background:#fde68a;color:#7c4a03;border:1px solid #f59e0b;}}
.band.s-add{{background:#bbf7d0;color:#146c43;border:1px solid #34d399;}}
.band.s-entry{{background:#bfdbfe;color:#1d4ed8;border:1px solid #60a5fa;}}
.band.s-near{{background:#fef08a;color:#854d0e;border:1px solid #eab308;}}
.band.s-watch{{background:#e5e7eb;color:#6b7280;border:1px solid #d1d5db;}}
.depth{{font-size:10px;color:var(--muted);margin-top:3px;}}
.c-tp{{min-width:132px;font-size:11px;}}
.tp{{font-weight:600;color:#1f2937;}}
.sep{{color:#d1d5db;margin:0 3px;}}
.thr{{font-size:9px;color:var(--muted);margin-top:3px;}}
.c-gap{{min-width:74px;font-size:11px;font-weight:600;}}
.c-pos{{min-width:64px;}}
.pctl{{font-weight:700;font-size:13px;}}
.ph,.pl{{font-size:9px;color:var(--muted);}}
.c-pe,.c-pb{{min-width:46px;color:#374151;}}
.c-cap{{min-width:44px;color:var(--sub);font-size:11px;}}
.tag-new{{display:inline-block;margin-left:4px;padding:0 4px;border-radius:7px;font-size:9px;font-weight:700;color:#fff;background:#ef4444;vertical-align:middle;}}
.tag-warn{{display:inline-block;margin-left:4px;padding:0 4px;border-radius:7px;font-size:9px;font-weight:600;color:#92400e;background:#fde68a;vertical-align:middle;}}
.tag-risk{{display:inline-block;margin-left:4px;padding:0 4px;border-radius:7px;font-size:9px;font-weight:700;color:#fff;background:#991b1b;vertical-align:middle;}}
.tag-ext{{display:inline-block;margin-left:4px;padding:0 4px;border-radius:7px;font-size:9px;font-weight:600;color:#3730a3;background:#e0e7ff;vertical-align:middle;}}
.lv-core{{color:#7c4a03;font-weight:700;}}
.lv-std{{color:#1d4ed8;font-weight:600;}}
.lv-watch{{color:#9ca3af;}}
.lv-special{{color:#7e22ce;font-weight:600;}}
.stbl{{width:100%;font-size:12px;min-width:0;}}
.stbl th,.stbl td{{padding:6px;border-bottom:1px solid var(--line);text-align:center;}}
.stbl th{{background:#f9fafb;font-size:11px;color:var(--sub);}}
.box.warn{{background:#fffbeb;border-color:#fcd34d;}}
.box.warn h3{{color:#92400e;}}
.box.warn ul{{margin:6px 0 0;padding-left:18px;}}
.box.warn li{{margin-bottom:3px;line-height:1.6;}}
.foot{{font-size:11px;color:var(--muted);padding:12px;line-height:1.8;text-align:center;}}
</style>
</head>
<body>
<div class="wrap">
  <h1>安全吃股息 · V3 策略扫描</h1>
  <div class="sub-title">
    依据《安全吃股息 V3.0》分类定价体系 · 股票池 {total} 只<br />
    更新时间 {now} · 数据源：腾讯实时行情 + 巨潮分红 + 上市公司财务摘要
  </div>

  <div class="cards">
    <div class="card"><div class="k">达到买点</div><div class="v red">{len(buy)}</div></div>
    <div class="card"><div class="k">接近买点</div><div class="v gold">{len(near)}</div></div>
    <div class="card"><div class="k">观察等待</div><div class="v">{len(watch)}</div></div>
    <div class="card"><div class="k">风控否决</div><div class="v green">{len(vetoed)}</div></div>
  </div>

  <div class="box">
    <h3>筛选顺序</h3>
    <b>质量 → DPS → 股息率门槛 → 价格 → 仓位 → 行动</b>，顺序不可颠倒。
    高股息只能触发研究，不能自动触发买入。<br />
    <b>定价 DPS</b> 按各股所属小类选定口径：银行/水电/高速/通信用保守 DPS（近三年最低），
    火电/航运用正常化 DPS（五年中位数、三年均值×80%、三年最低 取低）。<br />
    <b>三档价格</b> = 定价 DPS ÷ 目标股息率。达到首仓价才可用 30% 目标金额建仓，
    加仓价 30%，重仓价 40%。
  </div>

  <div class="box">
    <h3>档位与状态说明</h3>
    <div class="legend">
      <span class="band s-heavy">重仓档</span> 达到第三档
      <span class="band s-add">加仓档</span> 达到第二档
      <span class="band s-entry">首仓档</span> 达到第一档
      <span class="band s-near">接近买点</span> 距首仓线 15% 以内
      <span class="band s-watch">观察</span> 尚未触发
    </div>
  </div>
{flag_box}

  <div class="tw">
  <table>
    <thead><tr>
      <th class="c-idx">#</th>
      <th class="c-name">名称 / 分类</th>
      <th class="c-price">现价</th>
      <th class="c-yield">定价<br />股息率</th>
      <th class="c-dps">定价<br />DPS</th>
      <th class="c-band">档位</th>
      <th class="c-tp">首仓 / 加仓 / 重仓 价</th>
      <th class="c-gap">距离</th>
      <th class="c-pos">52周<br />分位</th>
      <th class="c-pe">PE</th>
      <th class="c-pb">PB</th>
      <th class="c-cap">仓位<br />上限</th>
    </tr></thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
  </div>

  <div class="box" style="margin-top:12px;">
    <h3>行业仓位纪律</h3>
    <table class="stbl">
      <thead><tr><th>大类</th><th>池内数量</th><th>达到买点</th><th>正常区间</th><th>上限</th></tr></thead>
      <tbody>
{sec_html}
      </tbody>
    </table>
    <div style="margin-top:8px;color:var(--sub);font-size:11px;">
      持有五家银行不等于已经充分分散。单股超限、行业超限时先停止加仓，而不是继续买入。
    </div>
  </div>

  <div class="foot">
    本页由脚本按公开规则自动生成，仅为投资方法演示，不构成任何买卖建议。<br />
    股息率门槛属于策略参数，执行前须核对公司最新公告与个人风险承受能力。
  </div>
</div>
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"      已生成 {out_path} ({len(html)/1024:.1f} KB)")


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.clear:
        ds.clear_cache()
        print("缓存已清空")

    df = scan(limit=args.limit)

    print("[3/4] 渲染报告 ...")
    render(df, OUT_DIR / "index.html")

    print("[4/4] 导出数据 ...")
    cols = [
        "code", "name", "cat", "sub", "level", "price", "pricing_yield",
        "static_yield", "pricing_dps", "dps_basis", "state", "state_label",
        "entry_price", "add_price", "heavy_price", "entry_y", "add_y", "heavy_y",
        "depth", "gap", "pe", "pb", "risk_level", "veto", "flags", "notes",
        "in_book",
    ]
    out_json = df[cols].to_dict(orient="records")
    (OUT_DIR / "data.json").write_text(
        json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    buy = df[df["state"].isin(["entry", "add", "heavy"])]
    print()
    print(f"=== 达到买点 {len(buy)} 只 / 全池 {len(df)} 只 ===")
    if len(buy):
        show = buy.sort_values("depth", ascending=False)[
            ["code", "name", "cat", "price", "pricing_yield", "pricing_dps",
             "state_label", "entry_price"]
        ]
        print(show.to_string(index=False))
    print()
    print("--- 接近买点 ---")
    near = df[df["state"] == "near"][
        ["code", "name", "cat", "price", "pricing_yield", "entry_price", "gap"]
    ]
    if len(near):
        print(near.to_string(index=False))
    else:
        print("（无）")


if __name__ == "__main__":
    main()
