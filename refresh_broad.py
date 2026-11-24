# -*- coding: utf-8 -*-
"""
全市场宽基指数基金(ETF)统计
覆盖: 沪深300 / 上证50 / 中证500 / 中证1000 / 中证2000 / 创业板 / 科创板50 / 中证A500
口径: 只统计纯跟踪该宽基指数的 ETF, 排除增强/成长/价值/红利/国证等衍生指数基金
净买入 = (当日份额 - 前一日份额) × 当日单位净值  (正=净申购, 负=净赎回)
数据源: 上交所 fund_etf_scale_sse / 深交所 fund_scale_daily_szse / 净值 东财 fund_open_fund_info_em
"""
import akshare as ak
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json, os, re, threading, time, requests

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(OUT_DIR, "全市场宽基指数基金统计.html")
SHARES_CACHE = os.path.join(OUT_DIR, "_broad_shares_cache.json")
NAV_CACHE = os.path.join(OUT_DIR, "_broad_nav_cache.json")
SITE_COPY = os.path.join(OUT_DIR, "broad.html")

HIST_DAYS = 250          # 展示窗口交易日数(缓存会逐日累积, 历史自动变长)
SH = "sh"
SZ = "sz"

# 宽基 → (指数代码, 成分ETF代码列表)
BROAD = {
    "沪深300": ("000300", ['510300', '510310', '159919', '510330', '515330', '510360', '159673', '515390',
                          '510380', '510350', '159330', '515380', '159300', '159925', '515660', '515310',
                          '510390', '159393', '515130', '510370', '515360', '515350']),
    "上证50": ("000016", ['510050', '510100', '530000', '510800', '530050', '510710', '510850',
                        '510190', '510680', '510600']),
    "中证500": ("000905", ['510500', '159922', '512500', '159820', '510580', '510510', '512510', '159337',
                          '515190', '159500', '159982', '510590', '561350', '159968', '510560', '510530',
                          '510570', '159935', '510550', '515550', '515530']),
    "中证1000": ("000852", ['512100', '560010', '159845', '159633', '159629', '560110', '516300']),
    "中证2000": ("932000", ['563300', '159531', '562660', '159532', '560220', '563200', '159536',
                          '561370', '159535', '159533']),
    "创业板": ("399006", ['159915', '159952', '159977', '159971', '159205', '159247', '159957',
                        '159948', '159908', '159958', '159964', '159821', '159810', '159956']),
    "科创板50": ("000688", ['588000', '588080', '588060', '588050', '588090', '588180', '588280', '588150']),
    "中证A500": ("000510", ['563360', '159352', '159338', '159361', '512050', '159351', '563220', '560610',
                          '159353', '560530', '560510', '159362', '159339', '563880', '159360', '159357',
                          '512020', '159215', '563500', '159358', '512080', '159359', '159356', '563860',
                          '159380', '159379', '563650', '159386', '560750', '563660', '159376']),
}
ALL_CODES = [c for _, codes in BROAD.values() for c in codes]
CODE_MKT = {c: (SH if c.startswith(("5", "6")) else SZ) for c in ALL_CODES}


def retry(fn, tries=3, gap=2):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(gap)
    raise last


def gen_cal_days(start_d, end_d):
    out, cur = [], start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def load_calendar(cache):
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    td = cache.get("trade_days") or {}
    days = sorted(td.get("days") or [])
    if days and days[-1] >= today:
        return set(days)
    try:
        df = ak.tool_trade_date_hist_sina()
        days = sorted({str(x)[:10].replace("-", "") for x in df["trade_date"]})
        if days:
            cache["trade_days"] = {"days": days}
            return set(days)
    except Exception as e:
        print("交易日历拉取失败(退化为工作日):", str(e)[:60], flush=True)
    return set(days) if days else None


def load_json(p, default):
    try:
        j = json.load(open(p, encoding="utf-8-sig"))
        return j if isinstance(j, dict) else default
    except Exception:
        return default


SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_HEAD = {"Referer": "https://www.sse.com.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
_tl = threading.local()


def _sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session()
        _tl.s = s
    return s


def sse_day(ds, sh_codes, name_map):
    """上交所单日 ETF 份额(万份→份). 自带超时, 可安全并发"""
    params = {"isPagination": "true", "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
              "pageHelp.beginPage": "1", "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
              "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
              "STAT_DATE": "%s-%s-%s" % (ds[:4], ds[4:6], ds[6:])}
    for _ in range(3):
        try:
            j = _sess().get(SSE_URL, params=params, headers=SSE_HEAD, timeout=(5, 20)).json()
            out = {}
            for it in (j.get("result") or []):
                c = str(it.get("SEC_CODE")).zfill(6)
                if c in sh_codes:
                    try:
                        out[c] = float(it.get("TOT_VOL")) * 10000
                        name_map[c] = str(it.get("SEC_NAME")).strip()
                    except Exception:
                        pass
            return out
        except Exception:
            time.sleep(1)
    return None


def fetch_shares(dates, name_map):
    """沪市逐日(上交所并发) + 深市区间批量(深交所), 只保留目标代码"""
    daily = {d: {} for d in dates}
    sh_codes = {c for c in ALL_CODES if CODE_MKT[c] == SH}
    sz_codes = {c for c in ALL_CODES if CODE_MKT[c] == SZ}

    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for ds, got in zip(dates, ex.map(lambda d: sse_day(d, sh_codes, name_map), dates)):
            if got:
                daily[ds].update(got)
                ok += 1
    print("沪市份额: %d/%d 日有数据" % (ok, len(dates)), flush=True)

    CH = 45
    for i in range(0, len(dates), CH):
        s, e = dates[i], dates[min(i + CH - 1, len(dates) - 1)]
        try:
            sdf = retry(lambda s=s, e=e: ak.fund_scale_daily_szse(start_date=s, end_date=e, symbol="ETF"))
        except Exception as ex:
            print("  深市批量失败 %s~%s: %s" % (s, e, str(ex)[:50]), flush=True)
            continue
        if sdf is None or len(sdf) == 0:
            continue
        for _, row in sdf.iterrows():
            dd = str(row.get("日期"))[:10].replace("-", "")
            c = str(row.get("基金代码")).zfill(6)
            if c in sz_codes and dd in daily:
                try:
                    daily[dd][c] = float(row.get("基金份额"))
                    name_map[c] = str(row.get("基金简称")).strip()
                except Exception:
                    pass
    print("深市份额: 完成", flush=True)
    return daily


def fetch_nav(codes, cache):
    """净值: 逐只拉全历史并写入缓存(缓存已有的只补缺失)"""
    hist = cache.setdefault("nav", {})
    todo = [c for c in codes if c not in hist or len(hist[c]) < 30]
    print("净值待拉取 %d 只(缓存 %d 只)" % (len(todo), len(codes) - len(todo)), flush=True)

    def _one(c):
        try:
            df = ak.fund_open_fund_info_em(symbol=c, indicator="单位净值走势")
            m = {}
            for _, r in df.iterrows():
                d = str(r.get("净值日期"))[:10].replace("-", "")
                try:
                    m[d] = float(r.get("单位净值"))
                except Exception:
                    pass
            return c, m
        except Exception:
            return c, None

    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, m in ex.map(_one, todo):
                if m:
                    hist[c] = m
    return hist


def nav_at(navmap, code, d):
    m = navmap.get(code) or {}
    if not m:
        return None
    if d in m:
        return m[d]
    prev = [k for k in m if k <= d]
    return m[max(prev)] if prev else None


def fmt_num(v, sign=False, dec=2):
    if v is None:
        return "—"
    return ("{:+,.%df}" % dec).format(v) if sign else ("{:,.%df}" % dec).format(v)


def html_cls(v):
    return "pos" if (v or 0) >= 0 else "neg"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1a1d24;--mut:#7a8194;--line:#e6e8ee;
  --pos:#d43d2a;--posbg:#fdecea;--neg:#1f9d7b;--negbg:#e6f6f0;--accent:#2f54eb;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  background:var(--bg);color:var(--ink);line-height:1.55;}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px;}
.top{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px;margin-bottom:18px;}
h1{font-size:22px;margin:0;font-weight:700}
.sub{color:var(--mut);font-size:12px;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
.sub b{font-weight:600;color:var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-auto-rows:1fr;gap:12px;margin-bottom:22px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;justify-content:center}
.kpi .lb{font-size:12px;color:var(--mut)}
.kpi .vl{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi .tg{font-size:12px;margin-top:2px}
.pos{color:var(--pos)} .neg{color:var(--neg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:22px}
.card h2{font-size:15px;margin:0 0 14px;font-weight:600}
.bars{display:flex;align-items:flex-end;gap:4px;height:190px;}
.col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;}
.bwrap{flex:1;display:flex;align-items:flex-end;width:100%}
.bar{width:100%;border-radius:3px;min-height:2px}
.bar.pos{background:linear-gradient(180deg,#e8836f,var(--pos))}
.bar.neg{background:linear-gradient(180deg,#54c2a6,var(--neg))}
.dt{font-size:10px;color:var(--mut);transform:rotate(-40deg);transform-origin:top right}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:12px;color:var(--mut);font-weight:500;text-align:right;padding:6px 8px;border-bottom:2px solid var(--line);white-space:nowrap}
th:first-child{text-align:left}
td{padding:7px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
td.r{text-align:right;white-space:nowrap}
tr.ctot td{border-top:2px solid var(--ink);font-weight:600;background:#fafbfc}
tr.ghead td{background:#eef2fa;color:#2b3a67;font-weight:700;font-size:11px;padding:5px 8px;border-top:1px solid var(--line)}
.mkt{display:inline-block;font-size:10px;color:var(--mut);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-right:6px}
.code{color:var(--mut);font-size:11px;margin-left:6px}
.tag{display:inline-block;font-size:10px;line-height:15px;color:#b45309;background:#fff7ed;
  border:1px solid #fed7aa;border-radius:4px;padding:0 5px;margin-left:6px;vertical-align:1px;font-weight:500;white-space:nowrap}
.tbar{width:90px}.tbf{background:#f0f1f4;border-radius:3px;height:10px;overflow:hidden}
.tbf i{display:block;height:100%;border-radius:3px}
.tbf i.pos{background:var(--pos)}.tbf i.neg{background:var(--neg)}
.note{color:var(--mut);font-size:12px;margin-top:18px;line-height:1.7}
.foot{color:var(--mut);font-size:11px;text-align:center;margin-top:30px}
a.jmp{color:var(--accent);text-decoration:none}
a.jmp:hover{text-decoration:underline}
"""


def render(p):
    dates, names = p["dates"], p["names"]
    day = p["day"]
    dd = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(str(d)) == 8 else str(d)

    def win_sum(code, n):
        ds = dates[-n:]
        return sum(p["net"].get(d, {}).get(code, 0) for d in ds)

    def grp_sum(cat, n):
        ds = dates[-n:]
        return sum(p["cat_net"].get(d, {}).get(cat, 0) for d in ds)

    tot1 = sum(p["cat_net"].get(day, {}).values())
    tot5 = sum(grp_sum(c, 5) for c in BROAD)
    tot20 = sum(grp_sum(c, 20) for c in BROAD)
    tot60 = sum(grp_sum(c, 60) for c in BROAD)

    h = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<title>全市场宽基指数基金统计</title><style>' + CSS + '</style></head><body><div class="wrap">']
    h.append('<div class="top"><div><h1>全市场宽基指数基金统计</h1><div class="sub">')
    h.append('<span>数据日 <b>%s</b></span><span>更新时间 <b>%s</b></span>' % (day, p["updated"]))
    h.append('<span>宽基 <b>%d</b> 类</span><span>ETF <b>%d</b> 只</span>' % (len(BROAD), len(ALL_CODES)))
    h.append('<span>区间 <b>%d</b> 个交易日</span>' % len(dates))
    h.append('</div></div></div>')

    h.append('<div class="kpis">')
    for lb, v, tg in [("宽基ETF当日合计净买入", tot1, "亿元"),
                      ("近5日累计", tot5, "亿元"),
                      ("近20日累计", tot20, "亿元"),
                      ("近60日累计", tot60, "亿元")]:
        h.append('<div class="kpi"><div class="lb">%s</div><div class="vl %s">%s</div><div class="tg">%s</div></div>'
                 % (lb, html_cls(v), fmt_num(v, True), tg))
    h.append('</div>')

    # 近21日柱状图
    chart_days = dates[-21:]
    vals = [p["all_net"].get(d, 0) for d in chart_days]
    mx = max([abs(x) for x in vals] + [1])
    h.append('<div class="card"><h2>近21个交易日 · 宽基ETF合计净买入（亿元）</h2><div class="bars">')
    for d, v in zip(chart_days, vals):
        pct = abs(v) / mx * 100
        h.append('<div class="col"><div class="bwrap"><div class="bar %s" style="height:%.1f%%" title="%s %s亿"></div></div>'
                 '<div class="dt">%s</div></div>' % (html_cls(v), max(pct, 1), dd(d), fmt_num(v, True), dd(d)[5:]))
    h.append('</div></div>')

    # 各宽基汇总
    h.append('<div class="card"><h2>各宽基汇总</h2><table><tr><th>宽基</th><th>指数</th><th class="r">只数</th>'
             '<th class="r">总份额(亿份)</th><th class="r">当日(亿)</th><th class="r">5日</th><th class="r">20日</th><th class="r">60日</th></tr>')
    for cat, (idx, codes) in BROAD.items():
        shs = sum(p["share"].get(day, {}).get(c, 0) for c in codes) / 1e8
        v1 = gsum = grp_sum(cat, 1)
        row = [shs, v1, grp_sum(cat, 5), grp_sum(cat, 20), grp_sum(cat, 60)]
        h.append('<tr><td><a class="jmp" href="#c_%s">%s</a></td><td class="code" style="margin:0">%s</td>'
                 '<td class="r">%d</td><td class="r">%s</td>' % (cat, cat, idx, len(codes), fmt_num(row[0])))
        for v in row[1:]:
            h.append('<td class="r %s">%s</td>' % (html_cls(v), fmt_num(v, True)))
        h.append('</tr>')
    h.append('</table></div>')

    # 明细
    for cat, (idx, codes) in BROAD.items():
        rows = [(c, p["share"].get(day, {}).get(c)) for c in codes if p["share"].get(day, {}).get(c)]
        rows.sort(key=lambda x: -x[1])
        mxshare = max([r[1] for r in rows] + [1])
        h.append('<div class="card" id="c_%s"><h2>%s <span class="code">%s</span> · %d只</h2><table>' % (cat, cat, idx, len(rows)))
        h.append('<tr><th>ETF</th><th class="r">份额(亿份)</th><th class="r">当日(亿)</th>'
                 '<th class="r">5日</th><th class="r">20日</th><th class="r">60日</th><th class="r">占比</th></tr>')
        for c, sv in rows:
            n1 = p["net"].get(day, {}).get(c, 0)
            n5 = win_sum(c, 5); n20 = win_sum(c, 20); n60 = win_sum(c, 60)
            nm = names.get(c, c)
            h.append('<tr><td><span class="mkt">%s</span>%s<span class="code">%s</span></td>'
                     '<td class="r">%s</td>' % ("沪" if CODE_MKT[c] == SH else "深", nm, c, fmt_num(sv / 1e8)))
            for v in (n1, n5, n20, n60):
                h.append('<td class="r %s">%s</td>' % (html_cls(v), fmt_num(v, True)))
            h.append('<td class="r tbar"><div class="tbf"><i class="pos" style="width:%.0f%%"></i></div></td></tr>'
                     % (sv / mxshare * 100))
        shs = sum(r[1] for r in rows) / 1e8
        h.append('<tr class="ctot"><td>合计 %d 只</td><td class="r">%s</td>' % (len(rows), fmt_num(shs)))
        for v in (grp_sum(cat, 1), grp_sum(cat, 5), grp_sum(cat, 20), grp_sum(cat, 60)):
            h.append('<td class="r %s">%s</td>' % (html_cls(v), fmt_num(v, True)))
        h.append('<td></td></tr></table></div>')

    h.append('<div class="note">')
    h.append('口径说明：净买入 = (当日份额 − 前一交易日份额) × 当日单位净值，正值为净申购（资金流入），负值为净赎回（资金流出）。<br>')
    h.append('数据来源：份额取自<b>上交所</b>（fund_etf_scale_sse）与<b>深交所</b>（fund_scale_daily_szse）官网披露，净值取自东方财富。<br>')
    h.append('成分口径：仅纳入纯跟踪该宽基指数的 ETF，<b>已排除增强型、成长/价值/红利/低波等风格型，以及国证2000 等其它指数</b>。<br>')
    h.append('T日份额在T日盘后至T+1早间陆续披露，若最新一日数据不完整可稍后刷新。')
    h.append('</div>')
    h.append('<div class="foot">数据每日自动更新 · 生成于 %s</div>' % p["updated"])
    h.append('</div></body></html>')
    return "".join(h)


def main():
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    cache = load_json(SHARES_CACHE, {})
    navcache = load_json(NAV_CACHE, {})
    cal = load_calendar(cache)

    hist = cache.setdefault("shares", {})
    cached_days = sorted(hist)
    if not cached_days:
        start = (now - timedelta(days=int(HIST_DAYS * 1.55))).replace(tzinfo=None)
    else:
        start = (datetime.strptime(cached_days[-1], "%Y%m%d") - timedelta(days=12))
    window = gen_cal_days(start, now.replace(tzinfo=None))
    if cal:
        window = [d for d in window if d in cal]
    window = window[-int(HIST_DAYS * 1.15):] if not cached_days else window
    print("拉取窗口: %s ~ %s (%d个工作日)" % (window[0], window[-1], len(window)) if window else "无窗口", flush=True)

    names = cache.setdefault("names", {})
    if window:
        fetched = fetch_shares(window, names)
        for d, m in fetched.items():
            if m:
                hist.setdefault(d, {}).update(m)
        if cal:
            last_cal = max(cal)
            for d in [x for x in list(hist) if x not in cal and x <= last_cal]:
                del hist[d]

    navmap = fetch_nav(ALL_CODES, navcache)

    dates = sorted(d for d in hist if hist[d])
    if not dates:
        print("无份额数据, 退出")
        return
    dates = dates[-HIST_DAYS:] if len(dates) > HIST_DAYS else dates
    print("有效交易日: %d 日 (%s ~ %s)" % (len(dates), dates[0], dates[-1]), flush=True)

    net, cat_net, all_net = {}, {}, {}
    for prev, d in zip(dates, dates[1:]):
        day_net = {}
        for c in ALL_CODES:
            s0, s1 = hist[prev].get(c), hist[d].get(c)
            nv = nav_at(navmap, c, d)
            if s0 and s1 and nv:
                day_net[c] = (s1 - s0) * nv / 1e8
        net[d] = day_net
        for cat, (_, codes) in BROAD.items():
            cat_net.setdefault(d, {})[cat] = round(sum(day_net.get(c, 0) for c in codes), 3)
        all_net[d] = round(sum(day_net.values()), 3)

    day = dates[-1]
    payload = {
        "dates": dates, "day": day, "names": names,
        "share": hist, "net": net, "cat_net": cat_net, "all_net": all_net,
        "updated": now.strftime("%Y-%m-%d %H:%M"),
    }
    html = render(payload)
    open(REPORT, "w", encoding="utf-8").write(html)
    open(SITE_COPY, "w", encoding="utf-8").write(html)
    json.dump(cache, open(SHARES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(navcache, open(NAV_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("报表已生成: %s" % REPORT, flush=True)
    print("最新数据日: %s, 宽基ETF当日净买入合计 %.2f亿" % (day, sum(cat_net.get(day, {}).values())), flush=True)


if __name__ == "__main__":
    main()
