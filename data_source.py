"""数据层：行情、分红、财务数据的获取与缓存。

数据源（全部免费、无需 key）：
  - 腾讯行情  http://qt.gtimg.cn/q=<codes>       实时价格 / PE / PB / 52周高低
  - 巨潮分红  ak.stock_dividend_cninfo           按报告年度归集的历史 DPS
  - 财务摘要  ak.stock_financial_abstract        分红支付率 / 现金流覆盖 / ROE / 负债率

所有网络结果落盘到 cache/ 目录，避免重复请求。
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_DAYS = 3  # 分红 / 财务数据的缓存有效期（天）
QUOTE_CACHE_MIN = 10  # 行情缓存有效期（分钟）

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------- 缓存工具
def _cache_get(name: str, ttl_seconds: float):
    p = CACHE_DIR / f"{name}.json"
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_seconds:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_put(name: str, obj):
    p = CACHE_DIR / f"{name}.json"
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def market_prefix(code: str) -> str:
    """沪市 6/9 开头，深市 0/3 开头，北交所 4/8 开头。"""
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return "bj"


def full_code(code: str) -> str:
    return market_prefix(code) + code


# ---------------------------------------------------------------- 实时行情
def fetch_quotes(codes) -> dict:
    """批量拉取腾讯行情。返回 {code: {price, pb, pe, high52, low52, change_pct}}"""
    cached = _cache_get("quotes", QUOTE_CACHE_MIN * 60)
    if cached and set(codes).issubset(cached.keys()):
        return {c: cached[c] for c in codes if c in cached}

    fc = [full_code(c) for c in codes]
    result = {}
    for i in range(0, len(fc), 50):
        batch = ",".join(fc[i : i + 50])
        try:
            r = requests.get(f"http://qt.gtimg.cn/q={batch}", timeout=20, headers=UA)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                line = line.strip()
                if not line:
                    continue
                m = re.search(r'v_\w+="([^"]+)"', line)
                if not m:
                    continue
                p = m.group(1).split("~")
                if len(p) < 53:
                    continue
                code = p[2]

                def f(i):
                    try:
                        return float(p[i])
                    except Exception:
                        return 0.0

                price, prev = f(3), f(4)
                if price <= 0:
                    continue
                result[code] = {
                    "price": price,
                    "change_pct": round((price - prev) / prev * 100, 2) if prev else 0.0,
                    "pb": f(46),
                    "pe": f(52) or f(53),
                    "pe_static": f(53),
                    "high52": f(47),
                    "low52": f(48),
                    "amount": f(37),  # 成交额（万元）
                }
        except Exception as e:
            print(f"   [行情] 批次失败: {e}")
        time.sleep(0.15)

    if result:
        _cache_put("quotes", result)
    return result


# ---------------------------------------------------------------- 分红历史
def fetch_dividend_years(code: str) -> dict:
    """按「报告年度」归集每股现金分红（元/股，含税）。

    数据源为巨潮 stock_dividend_cninfo，字段「派息比例」单位是 元/10股。
    年度分红 + 中期分红 + 季度分红按报告时间所属年份合并，剔除特别分红。

    复杂点处理：
      1. 「完整年度」判定 —— 只有该年度的年报分红已实施，才算分红年度完整。
         否则当年的中期分红会单独拎出来，被误判为「分红大幅下降」。
         例：2026 年只实施了一次中期分红，不能当作 2026 年度 DPS 与 2025 比较。
      2. 送股 / 转增 —— 会使历史每股分红在除权后被追溯放大，导致纵向不可比。
         对含送转的年度按送转比例做向后复权还原，保证与当前股本口径一致。
    """
    cached = _cache_get(f"div_{code}", CACHE_DAYS * 86400)
    if cached is not None:
        return {int(k): float(v) for k, v in cached.items()}

    import akshare as ak

    out = {}
    try:
        df = ak.stock_dividend_cninfo(symbol=code)
    except Exception:
        df = None

    if df is not None and len(df) > 0:
        # 先按年度收集明细，同时记录送股/转增累计比例
        records = []
        for _, r in df.iterrows():
            if "特别" in str(r.get("分红类型", "")):
                continue  # V3 规则：特别分红不计入定价 DPS
            m = re.match(r"(\d{4})", str(r.get("报告时间", "")))
            if not m:
                continue
            year = int(m.group(1))

            def _num(key):
                try:
                    v = float(r.get(key))
                    return v if v > 0 else 0.0
                except Exception:
                    return 0.0

            pay = _num("派息比例")
            song = _num("送股比例")  # 每股送股
            zhuan = _num("转增比例")  # 每股转增
            rtype = str(r.get("分红类型", ""))
            records.append(
                {
                    "year": year,
                    "pay": pay / 10.0,  # 元/10股 -> 元/股
                    "split": (song + zhuan) / 10.0,  # 每股新增股份数
                    "is_annual": "年度" in rtype,
                    "div_date": pd.to_datetime(r.get("除权日"), errors="coerce"),
                }
            )

        if not records:
            _cache_put(f"div_{code}", out)
            return out

        rdf = pd.DataFrame(records)

        # ---- 送转复权：把历史每股分红换算到「当前股本」口径
        # 某年发生 1:X 送转后，该年的分红本身已是送转前口径，需按之后各次送转放大。
        # 注意用 y < sy（严格小于）：当年分红在当年送转之前实施，不参与当年的放大。
        split_by_year = rdf.groupby("year")["split"].sum().to_dict()
        split_years = sorted([y for y, s in split_by_year.items() if s > 0])

        agg = {}
        for _, rec in rdf.iterrows():
            y = rec["year"]
            factor = 1.0
            for sy in split_years:
                if y < sy:  # 只按「之后发生」的送转放大
                    factor *= 1.0 + split_by_year[sy]
            agg[y] = agg.get(y, 0.0) + rec["pay"] * factor

        # ---- 完整性判定：年报分红已实施的年份才算完整
        annual_done = set(
            int(r["year"])
            for _, r in rdf[rdf["is_annual"]].iterrows()
            if pd.notna(r["div_date"])
        )
        # 只保留有年报实施的年度，避免把「只有中期分红」的当期当成年 DPS
        out = {y: round(v, 4) for y, v in agg.items() if y in annual_done and v > 0}

    _cache_put(f"div_{code}", out)
    return out


def dividend_detail(code: str) -> dict:
    """返回完整口径信息，供界面展示。

    {years: {年: dps}, complete_year: 最新完整年度, partial: 是否有未完成年度}
    """
    years = fetch_dividend_years(code)
    return {
        "years": years,
        "complete_year": max(years) if years else None,
    }


# ---------------------------------------------------------------- 财务摘要
def fetch_financials(code: str) -> dict:
    """提取年报口径的关键财务指标。

    返回 {year: {profit, ocf, eps, roe, debt_ratio, revenue}}
    profit / ocf 单位：元；roe / debt_ratio 单位：%
    """
    cached = _cache_get(f"fin_{code}", CACHE_DAYS * 86400)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}

    import akshare as ak

    out = {}
    try:
        df = ak.stock_financial_abstract(symbol=code)
    except Exception:
        df = None

    if df is not None and len(df) > 0:
        # 只取年报列（YYYY1231）
        year_cols = {}
        for c in df.columns:
            cs = str(c)
            if len(cs) == 8 and cs.endswith("1231") and cs.isdigit():
                year_cols[int(cs[:4])] = c

        want = {
            "profit": "归母净利润",
            "ocf": "经营现金流量净额",
            "eps": "基本每股收益",
            "roe": "净资产收益率(ROE)",
            "debt": "资产负债率",
            "revenue": "营业总收入",
        }
        for year, col in sorted(year_cols.items()):
            rec = {}
            for key, ind in want.items():
                sub = df[df["指标"] == ind]
                if len(sub) == 0:
                    continue
                try:
                    v = float(sub.iloc[0][col])
                except Exception:
                    continue
                if pd.notna(v):
                    rec[key] = v
            if rec:
                out[year] = rec

    _cache_put(f"fin_{code}", out)
    return out


def clear_cache():
    for p in CACHE_DIR.glob("*.json"):
        p.unlink()
