# -*- coding: utf-8 -*-
"""
国家队(中央汇金)核心宽基ETF 每日净买入/净卖出 自动刷新脚本
运行方式: python refresh_national_team_etf.py
每次运行: 重新拉取沪深两市 ETF 份额与净值, 计算每日净买卖, 生成自包含HTML报表
净买入/净卖出 = (当日份额 - 前一日份额) × 当日单位净值 (正=净买入, 负=净卖出)
"""
import akshare as ak
from datetime import datetime, timedelta
import json, time, os, io, sys, requests

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(OUT_DIR, "国家队ETF每日净买入卖出报表.html")
SHARES_CACHE = os.path.join(OUT_DIR, "_etf_shares_cache.json")

def gen_cal_days(start_d, end_d):
    """start_d..end_d 之间的工作日(周一~周五), 返回 YYYYMMDD 序列"""
    out, cur = [], start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out

def incremental_dates(cache, cal_days):
    """增量拉取窗口: 有缓存则只拉最近若干天(含补拉最近12天以便捕获晚发布), 无缓存则全量构建"""
    hist = (cache or {}).get("shares", {})
    today = datetime.now()
    if not hist:
        return natural_dates(cal_days)
    try:
        lastc = datetime.strptime(max(hist), "%Y%m%d")
    except Exception:
        lastc = today - timedelta(days=1)
    start = lastc + timedelta(days=1)
    refresh = today - timedelta(days=12)
    if start > refresh:
        start = refresh
    return gen_cal_days(start, today)

def load_shares_cached(window_dates, cache):
    """拉取窗口数据并合并进持久化缓存, 返回 (合并后shares, 所有有数据日期, deep_start)"""
    hist = cache.setdefault("shares", {})
    fetched, valid_fetch, ds_start = load_shares(window_dates)
    for d, m in fetched.items():
        if m:
            hist.setdefault(d, {}).update(m)
    if ds_start:
        cache["deep_start"] = ds_start
    cached_days = sorted(hist.keys())
    valid = sorted(d for d in cached_days if hist[d])
    return hist, valid, cache.get("deep_start")

TARGETS = [
    ("510300","沪深300ETF 华泰柏瑞","sh"),("510310","沪深300ETF 易方达","sh"),
    ("510330","沪深300ETF 华夏","sh"),("159919","沪深300ETF 嘉实","sz"),
    ("510050","上证50ETF 华夏","sh"),("510100","上证50ETF 易方达","sh"),
    ("510500","中证500ETF 南方","sh"),("512500","中证500ETF 华夏","sh"),
    ("159922","中证500ETF 嘉实","sz"),
    ("512100","中证1000ETF 南方","sh"),("159845","中证1000ETF 华夏","sz"),
    ("560010","中证1000ETF 广发","sh"),("159629","中证1000ETF 富国","sz"),
    ("159915","创业板ETF 易方达","sz"),("159952","创业板ETF 广发","sz"),
    ("159977","创业板ETF 天弘","sz"),
    ("588080","科创板50ETF 易方达","sh"),("588050","科创板50ETF 工银","sh"),
    ("510180","上证180ETF 华安","sh"),("510230","上证180金融ETF 国泰","sh"),
    ("159901","深证100ETF 易方达","sz"),
    ("515800","中证800ETF 汇添富","sh"),("560050","MSCI中国A50ETF 汇添富","sh"),
]

# 同类指数归集
CAT_MAP = {
    "510300":"沪深300","510310":"沪深300","510330":"沪深300","159919":"沪深300",
    "510050":"上证50","510100":"上证50",
    "510500":"中证500","512500":"中证500","159922":"中证500",
    "512100":"中证1000","159845":"中证1000","560010":"中证1000","159629":"中证1000",
    "159915":"创业板","159952":"创业板","159977":"创业板",
    "588080":"科创板50","588050":"科创板50",
    "510180":"上证180","510230":"上证180",
    "159901":"深证100",
    "515800":"中证800","560050":"MSCI中国A50",
}
CAL_DAYS = 1100  # 约3年(约786个工作日) 覆盖最大历史

def retry(fn, tries=3, gap=2):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            time.sleep(gap)
    raise RuntimeError(last)

def natural_dates(n):
    dates, d = [], datetime.now()
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return dates

def fetch_kline(dates):
    """直连东财拉取中证全指(000985)日K, 按 dates 对齐返回 [{d,o,h,l,c}|None]. 东财单次限约999条."""
    if not dates:
        return []
    start = dates[0].replace("-", ""); end = dates[-1].replace("-", "")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
           "&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=0&secid=1.000985"
           "&beg=%s&end=%s&smplmt=1000&lmt=200000" % (start, end))
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

    def _get():
        j = requests.get(url, headers=headers, timeout=45).json()
        if not isinstance(j, dict):
            raise ValueError("bad resp type")
        ks = (j or {}).get("data", {}).get("klines")
        if not ks:
            raise ValueError("empty klines")
        return ks

    data = retry(_get, tries=5, gap=6)
    m = {}
    for line in data:
        f = line.split(",")
        k = f[0].replace("-", "")
        if k.isdigit() and len(k) == 8:
            # 顺序: 日期,开,收,高,低,...
            m[k] = [round(float(f[1]), 3), round(float(f[3]), 3),
                    round(float(f[4]), 3), round(float(f[2]), 3)]
    if not m:
        return [None for _ in dates]
    return [({"d": d, "o": m[k][0], "h": m[k][1], "l": m[k][2], "c": m[k][3]}
             if (k := d.replace("-", "")) in m else None) for d in dates]

def fetch_kline_tx(dates):
    """备选源(腾讯) 中证全指(sh000985)日K, 与 fetch_kline 同返回格式. 行: 日期,开,收,高,低,量"""
    if not dates:
        return []
    start = dates[0].replace("-", ""); end = dates[-1].replace("-", "")
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=sh000985,day,%s,%s,1600,qfq" % (start, end))
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

    def _get():
        j = requests.get(url, headers=headers, timeout=45).json()
        if not isinstance(j, dict):
            raise ValueError("bad resp type")
        d = j.get("data")
        rows = None
        if isinstance(d, dict):
            for sym in ("sh000985", "qfqday", "day"):
                v = d.get(sym)
                if isinstance(v, dict):
                    rows = v.get("qfqday") or v.get("day") or v.get("data")
                elif isinstance(v, list) and v:
                    rows = v
                if rows:
                    break
        elif isinstance(d, list) and d:
            rows = d
        if not rows:
            raise ValueError("empty tx klines")
        return rows

    rows = retry(_get, tries=8, gap=8)
    m = {}
    for f in rows:
        try:
            if len(f) >= 5 and f[0].isdigit() is False and "-" in f[0]:
                k = f[0].replace("-", "")
                m[k] = [round(float(f[1]), 3), round(float(f[3]), 3),
                        round(float(f[4]), 3), round(float(f[2]), 3)]
        except Exception:
            pass
    return [({"d": d, "o": m[k][0], "h": m[k][1], "l": m[k][2], "c": m[k][3]}
             if (k := d.replace("-", "")) in m else None) for d in dates]

def load_shares(dates):
    """沪: fund_etf_scale_sse 逐日; 深: fund_scale_daily_szse 区间批量"""
    sh_codes = [c for c, _, mk in TARGETS if mk == "sh"]
    sz_codes = [c for c, _, mk in TARGETS if mk == "sz"]
    daily_shares = {ds: {} for ds in dates}
    valid = []
    for ds in dates:
        try:
            df = retry(lambda ds=ds: ak.fund_etf_scale_sse(date=ds))
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                try:
                    daily_shares[ds][str(row.get("基金代码")).zfill(6)] = float(row.get("基金份额"))
                except Exception:
                    pass
            valid.append(ds)
        except Exception as e:
            print(f"  沪 fail {ds}: {str(e)[:70]}", flush=True)
    valid.sort()
    deep_start = None
    if sz_codes:
        sz_days = sorted(dates)
        CH = 45
        for i in range(0, len(sz_days), CH):
            s = sz_days[i]; e = sz_days[min(i + CH - 1, len(sz_days) - 1)]
            try:
                sdf = retry(lambda s=s, e=e: ak.fund_scale_daily_szse(start_date=s, end_date=e, symbol="ETF"))
            except Exception as ex:
                print(f"  深批 fail {s}~{e}: {str(ex)[:50]}", flush=True); continue
            if sdf is None or len(sdf) == 0:
                continue
            for _, row in sdf.iterrows():
                dd = str(row.get("日期"))[:10].replace("-", "")
                try:
                    code = str(row.get("基金代码")).zfill(6); v = float(row.get("基金份额"))
                except Exception:
                    continue
                if code in sz_codes:
                    daily_shares.setdefault(dd, {})[code] = v
        sz_have = sorted(d for d, m in daily_shares.items() if any(c in m for c in sz_codes))
        if sz_have:
            valid = sorted(set(valid) | set(sz_have))
            deep_start = sz_have[0]
    return daily_shares, valid, deep_start

def load_nav():
    nav = {}
    for code, _, _ in TARGETS:
        try:
            info = retry(lambda c=code: ak.fund_open_fund_info_em(symbol=c, indicator="单位净值走势"))
            m = {}
            for _, row in info.iterrows():
                d = str(row.get("净值日期"))[:10].replace("-", "")
                try:
                    m[d] = float(row.get("单位净值"))
                except Exception:
                    pass
            nav[code] = m
        except Exception as e:
            print(f"  净值 fail {code}: {str(e)[:70]}", flush=True)
    return nav

def main():
    print("拉取数据中 ...", flush=True)
    cache = {}
    if os.path.exists(SHARES_CACHE):
        try:
            with open(SHARES_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception as e:
            print("缓存损坏, 将全量重建:", e, flush=True)
            cache = {}
    window = incremental_dates(cache, CAL_DAYS)
    print("本次拉取日期窗口: %s ~ %s (共%d个工作日)" %
          (window[0] if window else "-", window[-1] if window else "-", len(window)), flush=True)
    shares, valid, deep_start = load_shares_cached(window, cache)
    with open(SHARES_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    if not valid:
        print("未取到任何交易日份额数据"); return 1
    nav = load_nav()

    daily = []
    for code, name, mkt in TARGETS:
        prev = None; lnav = None
        for ds in valid:
            v = shares.get(ds, {}).get(code)
            if v is None:
                continue
            nn = nav.get(code, {}).get(ds)
            if nn is not None:
                lnav = nn
            net = (v - prev) * lnav if (prev is not None and lnav is not None) else None
            daily.append({"d": ds, "c": code, "n": name, "m": mkt, "s": v,
                          "w": lnav, "f": net})
            prev = v
        # 缺失份额时补全当日0? 不处理, 汇总时按实际值

    # 建立 日期->每条, 日期->合计
    by_date = {}
    items_by = {}
    for it in daily:
        items_by.setdefault(it["d"], {})[it["c"]] = it
    dates_sorted = sorted({it["d"] for it in daily})
    date_total = {}
    for ds in dates_sorted:
        s = sum(x["f"] for x in items_by[ds].values() if x["f"] is not None)
        date_total[ds] = s

    today = dates_sorted[-1]
    week_days = dates_sorted[-5:]
    month_days = dates_sorted[-21:]

    def cum(days):
        dc = {x["c"]: 0.0 for x in daily}
        for ds in days:
            for c, it in items_by.get(ds, {}).items():
                if it["f"] is not None:
                    dc[c] += it["f"]
        return dc
    w_sum = cum(week_days); m_sum = cum(month_days)

    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_asof": today,
        "deep_start": deep_start,
        "dates": [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates_sorted],
        "date_total": {k: round(v / 1e8, 3) for k, v in date_total.items()},
        "etfs": [],
    }
    try:
        payload["kline"] = fetch_kline(dates_sorted)
        print("中证全指K线已更新", flush=True)
    except Exception as e:
        print("东财K线失败, 尝试腾讯源:", e)
        try:
            payload["kline"] = fetch_kline_tx(dates_sorted)
            print("腾讯K线已更新", flush=True)
        except Exception as e2:
            print("K线拉取失败(报表其余部分不受影响):", e2)
            payload["kline"] = [None] * len(dates_sorted)
    for code, name, mkt in TARGETS:
        payload["etfs"].append({
            "c": code, "n": name, "m": "沪" if mkt == "sh" else "深",
            "cat": CAT_MAP.get(code, "其他"),
            "today": (items_by.get(today, {}).get(code, {}).get("f") or 0) / 1e8,
            "week": round(w_sum.get(code, 0) / 1e8, 3),
            "month": round(m_sum.get(code, 0) / 1e8, 3),
            "last_share": (lambda c=code: (items_by.get(today, {}).get(c, {}).get("s") or 0) / 1e8)(),
            "last_nav": (items_by.get(today, {}).get(code, {}).get("w") or 0),
            "series": {f"{ds[:4]}-{ds[4:6]}-{ds[6:]}": round((items_by.get(ds, {}).get(code, {}).get("f") or 0) / 1e8, 3)
                       for ds in dates_sorted},
        })
    # 每日总持仓市值(全市场口径, 亿元): 每只 份额×净值, 缺日用前一交易日前值填充
    per_day = {}
    for it in daily:
        per_day.setdefault(it["c"], {})[it["d"]] = (it["s"], it["w"])
    last_seen = {}
    mv_total = {}
    for ds in dates_sorted:
        tot = 0.0
        for c, _, _ in TARGETS:
            pd = per_day.get(c, {})
            if ds in pd:
                last_seen[c] = pd[ds]
            s, w = last_seen.get(c, (None, None))
            if s is not None and w is not None:
                tot += s * w
        mv_total[ds] = round(tot / 1e8, 1)
    payload["mv_total"] = mv_total
    return payload

def escape_html(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def render_html(p):
    def fnum(v, sign=False):
        if v is None:
            return "—"
        s = f"{v:+,.2f}" if sign else f"{v:,.2f}"
        return s
    def cls(v):
        return "pos" if v >= 0 else "neg"
    dates = p["dates"]
    dt = {}
    for k, v in p["date_total"].items():
        kk = str(k)
        if len(kk) == 8:
            kk = f"{kk[:4]}-{kk[4:6]}-{kk[6:]}"
        dt[kk] = v
    # 近21交易日用于柱状图
    chart = dates[-21:]
    chart_total = [dt.get(d, 0) for d in chart]
    maxabs = max([abs(x) for x in chart_total] + [1])
    # 近一年滚动累计 (曲线), 起点对齐到深市有数据的日期
    deep_start = p.get("deep_start")
    if deep_start and len(str(deep_start)) == 8:
        ds = str(deep_start)
        deep_start = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
    bidx_sz = dates.index(deep_start) if deep_start in dates else 0
    bidx = bidx_sz  # 用尽深市可用历史, 曲线取最长
    y_dates = dates[bidx:]
    _mv_src = p.get("mv_total", {})
    _mvm = {}
    for _k, _v in _mv_src.items():
        _kk = str(_k)
        if len(_kk) == 8:
            _kk = f"{_kk[:4]}-{_kk[4:6]}-{_kk[6:]}"
        _mvm[_kk] = _v
    y_mv = [_mvm.get(d, 0) for d in y_dates]
    # 近一周 / 近一月
    week_days = dates[-5:]
    month_days = dates[-21:]
    week_tot = sum(dt.get(d, 0) for d in week_days)
    month_tot = sum(dt.get(d, 0) for d in month_days)
    today_tot = dt.get(dates[-1], 0)

    # --- 中证全指K线联动数据 ---
    kline = p.get("kline") or [None] * len(dates)
    K_ARR = json.dumps([x for x in kline], ensure_ascii=False)
    Q_ARR = json.dumps([round(dt.get(d, 0), 3) for d in dates])
    D_ARR = json.dumps(dates, ensure_ascii=False)
    KLN_JS = ("<script>\n"
      "var DATA_K=" + K_ARR + ";\n"
      "var DATA_Q=" + Q_ARR + ";\n"
      "var DATA_D=" + D_ARR + ";\n"
      "(function(){\n"
      "function go(){\n"
      " var box=document.getElementById('klnBox');\n"
      " if(!window.echarts){var t=document.getElementById('klnTip');if(t)t.textContent='图表库未加载(需联网)';return;}\n"
      " var chart=echarts.init(box),cur=120;\n"
      " function opt(n){\n"
      "  var s=(n>0)?Math.max(0,DATA_D.length-n):0,tail=(s>0)?{startValue:s,endValue:DATA_D.length-1} : null;\n"
      "  var ds=DATA_D.slice(s),ks=DATA_K.slice(s),qs=DATA_Q.slice(s);\n"
      "  return {axisPointer:{link:[{xAxisIndex:[0,1]}],lineStyle:{color:'#98a2b8'}},\n"
      "   grid:[{left:50,right:56,top:16,height:300,gridIndex:0},{left:50,right:56,bottom:10,height:130,gridIndex:1}],\n"
      "   xAxis:[{type:'category',data:ds,gridIndex:0,boundaryGap:true,axisLine:{lineStyle:{color:'#c9cfdd'}},axisLabel:{color:'#7a8194',fontSize:10},axisTick:{show:false}},\n"
      "          {type:'category',data:ds,gridIndex:1,axisLine:{lineStyle:{color:'#c9cfdd'}},axisLabel:{show:false},axisTick:{show:false}}],\n"
      "   yAxis:[{scale:true,gridIndex:0,position:'right',splitLine:{lineStyle:{color:'#eef1f6'}},axisLabel:{color:'#7a8194',fontSize:10}},\n"
      "          {scale:true,gridIndex:1,position:'right',splitLine:{show:false},axisLabel:{color:'#7a8194',fontSize:10,formatter:function(v){return v.toFixed(0);}}}],\n"
      "   series:[\n"
      "    {name:'中证全指',type:'candlestick',data:ks.map(function(x){return x?[x.o,x.c,x.l,x.h]:[null,null,null,null];}),\n"
      "     itemStyle:{color:'#d43d2a',color0:'#1f9d7b',borderColor:'#d43d2a',borderColor0:'#1f9d7b'}},\n"
      "    {name:'当日净买卖',type:'bar',xAxisIndex:1,yAxisIndex:1,data:qs,\n"
      "     itemStyle:{color:function(p){return p.value>=0?'#d43d2a':'#1f9d7b';}}}\n"
      "   ],\n"
      "   tooltip:{trigger:'axis',axisPointer:{type:'cross'},position:[8,338],confine:true,backgroundColor:'#2a2f3a',borderWidth:0,textStyle:{color:'#fff',fontSize:12},extraCssText:'white-space:nowrap;max-width:880px;padding:6px 10px;box-shadow:0 2px 8px rgba(0,0,0,.15);',\n"
      "    formatter:function(ps){var i=ps[0].dataIndex,g=i+s,d=DATA_D[g],k=DATA_K[g]||{},q=DATA_Q[g];\n"
      "     var s1='';\n"
      "     if(k.o!=null){s1=' &nbsp;开'+k.o+' 高'+k.h+' 低'+k.l+' 收'+k.c;}\n"
      "     var col=q>=0?'#f0705c':'#46c7a6';\n"
      "     return '<b>'+d+'</b>'+s1+' &nbsp;当日净买卖 <b style=\"color:'+col+'\">'+(q>=0?'+':'')+q.toFixed(2)+' 亿</b>';}}\n"
      "  };\n"
      " }\n"
      " chart.setOption(opt(cur));\n"
      " var btns=document.querySelectorAll('.rng');\n"
      " Array.prototype.forEach.call(btns,function(b){b.addEventListener('click',function(){\n"
      "   cur=parseInt(b.getAttribute('data-n'),10)||120;\n"
      "   Array.prototype.forEach.call(btns,function(x){x.classList.toggle('sel',x===b);});\n"
      "   chart.setOption(opt(cur));\n"
      " });});\n"
      " window.addEventListener('resize',function(){chart.resize();});\n"
      "}\n"
      "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',go);}else{go();}\n"
      "})();\n"
      "</script>")

    # 内联 ECharts，避免境外 CDN 在本机加载失败导致图表空白
    _EC_PATH = r"C:\Users\wxk11\.trae-cn\memory\echarts.min.js"
    if os.path.exists(_EC_PATH) and os.path.getsize(_EC_PATH) > 100000:
        _ec = io.open(_EC_PATH, encoding="utf-8").read().replace("</script>", "<\\/script>")
        ECHARTS_HTML = "<script>" + _ec + "</script>"
    else:
        ECHARTS_HTML = ('<script src="https://registry.npmmirror.com/echarts/5.5.0/files/dist/echarts.min.js"></script>')

    etfs = sorted(p["etfs"], key=lambda e: e["month"])

    # 同类(指数)品种归集
    agg = {}
    for e in etfs:
        agg.setdefault(e.get("cat", "其他"), []).append(e)
    cat_rows_html = ""
    cat_share_tot = cat_today_tot = cat_week_tot = cat_month_tot = cat_mv_tot = 0.0
    for cat in sorted(agg, key=lambda c: sum(x["month"] for x in agg[c]), reverse=True):
        es = agg[cat]
        td = sum(x["today"] for x in es); wk = sum(x["week"] for x in es)
        mo = sum(x["month"] for x in es); sh = sum(x["last_share"] for x in es)
        mv = sum(x["last_share"] * x["last_nav"] for x in es)
        cat_today_tot += td; cat_week_tot += wk; cat_month_tot += mo; cat_share_tot += sh; cat_mv_tot += mv
        w = min(abs(mo) / 200 * 100, 100)
        cat_rows_html += f"""<tr>
          <td><b>{escape_html(cat)}</b><span class="code">{len(es)}只</span></td>
          <td class="r {cls(td)}">{fnum(td, True)}</td>
          <td class="r {cls(wk)}">{fnum(wk, True)}</td>
          <td class="r {cls(mo)}">{fnum(mo, True)}</td>
          <td class="r">{sh:,.1f}</td>
          <td class="r">{mv:,.1f}</td>
          <td class="tbar"><div class="tbf"><i class="{cls(mo)}" style="width:{w:.0f}%"></i></div></td></tr>"""
    cat_tot_row = f"""<tr class="ctot"><td><b>全部（{len(agg)}类{len(etfs)}只）</b></td>
      <td class="r {cls(cat_today_tot)}">{fnum(cat_today_tot, True)}</td>
      <td class="r {cls(cat_week_tot)}">{fnum(cat_week_tot, True)}</td>
      <td class="r {cls(cat_month_tot)}">{fnum(cat_month_tot, True)}</td>
      <td class="r">{cat_share_tot:,.1f}</td>
      <td class="r">{cat_mv_tot:,.1f}</td><td></td></tr>"""

    # 每日明细矩阵 (最近 HOT 个交易日)
    HOT = 10
    hot_dates = dates[-HOT:]
    hot_html = ""
    mcol_headers = "".join(f"<th>{d[5:]}</th>" for d in hot_dates)
    for e in etfs:
        ser = e["series"]
        cells = ""
        for d in hot_dates:
            v = ser.get(d)
            if v is None:
                cells += '<td class="r" style="color:var(--mut)">—</td>'; continue
            if abs(v) < 0.001:
                cells += '<td class="r zero" title="当日份额较前日无变化(申赎相抵)">0</td>'; continue
            cells += f'<td class="r {cls(v)}" title="{d} 位">{fnum(v, True)}</td>'
        hot_html += f"<tr><td><span class='mkt'>{e['m']}</span>{escape_html(e['n'])}</td>{cells}</tr>"

    # 表格行
    rows = ""
    for e in etfs:
        rows += f"""<tr>
          <td><span class="mkt">{e['m']}</span>{escape_html(e['n'])}<span class="code">{e['c']}</span></td>
          <td class="r">{e['last_share']:,.1f}</td>
          <td class="r {cls(e['today'])}">{fnum(e['today'], True)}</td>
          <td class="r {cls(e['week'])}">{fnum(e['week'], True)}</td>
          <td class="r {cls(e['month'])}">{fnum(e['month'], True)}</td>
          <td class="tbar"><div class="tbf"><i class="{cls(e['month'])}" style="width:{min(abs(e['month'])/50*100,100):.0f}%"></i></div></td>
        </tr>"""

    # 柱状图
    bars = ""
    for i, d in enumerate(chart):
        v = chart_total[i]
        h = abs(v) / maxabs * 100
        bars += f"""<div class="col" title="{d} {fnum(v,True)}亿">
          <div class="bwrap"><div class="bar {cls(v)}" style="height:{max(h,1.5):.1f}%"></div></div>
          <div class="dt">{d[5:]}</div></div>"""

    # 累计持仓金额折线(SVG)
    W, H = 860, 120; pad = 12
    mn, mx = min(y_mv), max(y_mv)
    if mn == mx: mn -= 1; mx += 1
    npts = len(y_mv)
    pts = []
    for i, v in enumerate(y_mv):
        x = pad + i * (W - 2 * pad) / (npts - 1)
        y = H - pad - (v - mn) / (mx - mn) * (H - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    last_pts = pts[-1]; last_v = y_mv[-1]
    ypt_json = json.dumps([{"d": y_dates[i], "v": round(y_mv[i], 1)} for i in range(len(y_dates))], ensure_ascii=False)
    JS_CODE = ("<script>\n"
      "const YPT=" + ypt_json + ";\n"
      "const C0=" + str(pad) + ",C1=" + str(W - pad) + ",CH=" + str(H) +
      ",CMN=" + f"{mn:.0f}" + ",CMX=" + f"{mx:.0f}" + ",WW=" + str(W) + ";\n"
      "const CVD=document.getElementById('cumSvg'),TIP=document.getElementById('cumTip'),G=document.getElementById('cumGuide');\n"
      "const N=YPT.length;\n"
      "function xAt(i){return C0+i*(C1-C0)/(N-1);}\n"
      "function FMT(v){return v.toFixed(1)+'亿';}\n"
      "CVD.addEventListener('mousemove',function(e){"
      "var r=CVD.getBoundingClientRect();"
      "var sx=(e.clientX-r.left)/r.width*WW;"
      "var i=Math.round((sx-C0)/(C1-C0)*(N-1));"
      "if(i<0)i=0;if(i>N-1)i=N-1;"
      "var p=YPT[i],gx=xAt(i);"
      "G.setAttribute('x1',gx);G.setAttribute('x2',gx);"
      "TIP.style.color='var(--accent)';"
      "TIP.textContent='日期 '+p.d+'  持仓金额 '+FMT(p.v);"
      "});"
      "CVD.addEventListener('mouseleave',function(){G.setAttribute('x1',-10);G.setAttribute('x2',-10);});"
      "\n</script>")

    def fmt_dt(d):
        return f"{d[0:4]}-{d[5:7]}-{d[8:10]}"

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>国家队ETF每日净买入卖出报表</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1a1d24;--mut:#7a8194;--line:#e6e8ee;
  --pos:#d43d2a;--posbg:#fdecea;--neg:#1f9d7b;--negbg:#e6f6f0;--accent:#2f54eb;}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  background:var(--bg);color:var(--ink);line-height:1.55;}}
.wrap{{max-width:980px;margin:0 auto;padding:20px 16px 60px;}}
.top{{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px;margin-bottom:18px;}}
h1{{font-size:22px;margin:0;font-weight:700}}
.sub{{color:var(--mut);font-size:12px;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}}
.sub b{{font-weight:600;color:var(--accent)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.kpi .lb{{font-size:12px;color:var(--mut)}}
.kpi .vl{{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}}
.kpi .tg{{font-size:12px;margin-top:2px}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:22px}}
.card h2{{font-size:15px;margin:0 0 14px;font-weight:600}}
.bars{{display:flex;align-items:flex-end;gap:4px;height:190px;}}
.col{{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;}}
.bwrap{{flex:1;display:flex;align-items:flex-end;width:100%}}
.bar{{width:100%;border-radius:3px;min-height:2px}}
.bar.pos{{background:linear-gradient(180deg,#e8836f,var(--pos))}}
.bar.neg{{background:linear-gradient(180deg,#54c2a6,var(--neg))}}
.dt{{font-size:10px;color:var(--mut);transform:rotate(-40deg);transform-origin:top right}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{font-size:12px;color:var(--mut);font-weight:500;text-align:right;padding:6px 8px;border-bottom:2px solid var(--line);white-space:nowrap}}
th:first-child{{text-align:left}}
td{{padding:7px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}}
td.r{{text-align:right;white-space:nowrap}}
tr.ctot td{{border-top:2px solid var(--ink);font-weight:600;background:#fafbfc}}
td.zero{{color:var(--mut);font-size:11px;opacity:.65}}
table.matrix td{{font:11px var(--font-mono,"SFMono-Regular",Consolas,monospace)}}
table.matrix th{{font-size:10px}}
.mkt{{display:inline-block;font-size:10px;color:var(--mut);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-right:6px}}
.code{{color:var(--mut);font-size:11px;margin-left:6px}}
.tbar{{width:90px}}.tbf{{background:#f0f1f4;border-radius:3px;height:10px;overflow:hidden}}
.tbf i{{display:block;height:100%;border-radius:3px}}
.tbf i.pos{{background:var(--pos)}}.tbf i.neg{{background:var(--neg)}}

.note{{color:var(--mut);font-size:12px;margin-top:18px;line-height:1.7}}
.svgwrap{{margin-top:16px}}
.chartbox{{position:relative}}
.cumtip{{display:block;text-align:center;margin:2px auto 4px;min-height:18px;
  border:none;box-shadow:none;background:transparent;color:var(--mut);
  font:12px "SFMono-Regular",Consolas,monospace;pointer-events:none;white-space:nowrap}}
svg text{{fill:var(--mut);font-size:10px}}
.foot{{color:var(--mut);font-size:11px;text-align:center;margin-top:30px}}
.klnctl{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
.klnctl .kp{{font-size:12px;color:var(--mut)}}
.rng{{border:1px solid var(--line);background:#fff;border-radius:8px;padding:5px 12px;font-size:12px;cursor:pointer;color:var(--ink)}}
.rng:hover{{border-color:var(--accent)}}
.rng.sel{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.kpt{{font-size:12px;color:var(--mut);margin-left:auto;font-family:SFMono-Regular,Consolas,monospace}}
.ktip{{font-size:12px;color:var(--mut);text-align:center;margin-top:8px;min-height:16px;font-family:SFMono-Regular,Consolas,monospace}}
</style>
  {ECHARTS_HTML}
  </head><body><div class="wrap">
  <div class="top">
    <div>
      <h1>国家队ETF · 每日净买入 / 净卖出</h1>
      <div class="sub"><span>数据日 <b>{escape_html(p['data_asof'])}</b></span>
        <span>更新时间 <b>{escape_html(p['updated'])}</b></span>
        <span>标的口径：中央汇金持仓宽基ETF 共 <b>{len(etfs)}</b> 只</span></div>
    </div>
  </div>
  <div class="kpis">
    <div class="kpi"><div class="lb">最新交易日 {fmt_dt(dates[-1])} 合计</div>
      <div class="vl {cls(today_tot)}">{fnum(today_tot, True)}</div>
      <div class="tg {cls(today_tot)}">单位：亿元</div></div>
    <div class="kpi"><div class="lb">近一周累计 (5个交易日)</div>
      <div class="vl {cls(week_tot)}">{fnum(week_tot, True)}</div>
      <div class="tg {cls(week_tot)}">{ "净买入" if week_tot>=0 else "净卖出" }</div></div>
    <div class="kpi"><div class="lb">近一月累计 (21个交易日)</div>
      <div class="vl {cls(month_tot)}">{fnum(month_tot, True)}</div>
      <div class="tg {cls(month_tot)}">{ "净买入" if month_tot>=0 else "净卖出" }</div></div>
    <div class="kpi"><div class="lb">近一月全部品种合计</div>
      <div class="vl {cls(month_tot)}">{fnum(month_tot, True)}亿元</div>
      <div class="tg">所有品种加总</div></div>
  </div>
  <div class="card"><h2>全部品种 · 每日净买卖合计（近21个交易日，亿元）</h2>
    <div class="bars">{bars}</div>
    <div class="svgwrap"><div class="chartbox">
      <h2 style="font-size:14px">累计持仓金额 · 最长 {len(y_dates)}个交易日（亿元 · 悬停查看当日）</h2>
      <div id="cumTip" class="cumtip"></div>
      <svg id="cumSvg" viewBox="0 0 {W} {H}" width="100%" height="{H}" preserveAspectRatio="none">
        <line id="cumGuide" x1="-10" y1="{pad}" x2="-10" y2="{H-pad}" stroke="#c0c6d4" stroke-width="1" stroke-dasharray="3 3"/>
        <polyline points="{poly}" fill="none" stroke="#2f54eb" stroke-width="1.5"/>
        <circle cx="{last_pts.split(',')[0]}" cy="{last_pts.split(',')[1]}" r="3.5" fill="#2f54eb"/>
        <text x="{pad}" y="{pad-4}">{mn:.0f}</text>
        <text x="{W-pad}" y="{pad-4}" text-anchor="end">{mx:.0f}亿</text>
        <text x="{W-pad}" y="{H-6}" text-anchor="end">{last_v:,.0f}亿</text>
      </svg>
    </div></div>
    {JS_CODE}
  </div>
  <div class="card"><h2>中证全指K线 × 每日净买入联动（上K线 下当日净买卖 · 悬停查看）</h2>
    <div class="klnctl"><span class="kp">显示范围</span>
      <button class="rng" data-n="60">60日</button>
      <button class="rng sel" data-n="120">120日</button>
      <button class="rng" data-n="250">250日</button>
      <button class="rng" data-n="0">全部</button>
      <span class="kpt" id="klnDate"></span></div>
    <div id="klnBox" style="height:520px;width:100%"></div>
    <div id="klnTip" class="ktip">中证全指与国家队ETF每日净买卖按交易日对齐，鼠标悬停可联动查看当天K线与净买卖，红=净买入、绿=净卖出。</div>
  </div>
  <div class="card"><h2>分品种汇总 · 近一周 / 近一月净买入卖出（亿元，按近一月降序）</h2>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>品种</th><th>最新份额(亿)</th><th>今日</th><th>近一周</th><th>近一月</th><th style="width:90px">近一月强度</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
  </div>
  <div class="card"><h2>同类指数品种归集汇总（亿元）</h2>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>指数类别</th><th>今日</th><th>近一周</th><th>近一月</th><th>最新份额(亿)</th><th>持仓市值(亿)</th><th style="width:90px">近一月强度</th></tr></thead>
      <tbody>{cat_rows_html}{cat_tot_row}</tbody>
    </table></div>
    <div style="color:var(--mut);font-size:11px;margin-top:8px">将跟踪同一指数的多只 ETF 归集加总，观察该指数方向上的整体净买卖。</div>
  </div>
  <div class="card"><h2>每日净买卖明细（最近{HOT}个交易日，亿元 · 可横向滚动）</h2>
    <div style="overflow-x:auto"><table class="matrix">
      <thead><tr><th>品种</th>{mcol_headers}</tr></thead>
      <tbody>{hot_html}</tbody>
    </table></div>
    <div style="color:var(--mut);font-size:11px;margin-top:8px">「0」表示该日基金份额较上一交易日无变化（申购与赎回相抵），属正常现象，非数据缺失。</div>
  </div>
  <div class="note"><b>口径说明：</b>净买入 / 净卖出 =（当日基金份额 − 前一日基金份额）× 当日单位净值，正值=净买入(净申购)，负值=净卖出(净赎回)。
  份额源：上交所 〈AKShare fund_etf_scale_sse(逐日)〉、深交所 〈fund_scale_daily_szse(区间批量)〉；净值源：天天基金 〈fund_open_fund_info_em〉。
  份额为日终口径，当日净买卖反映当日申购赎回结果。数据仅为展示来源，不构成投资建议。</div>
  <div class="foot">Generated by TRAE · 每日运行脚本自动刷新</div>
  {KLN_JS}
</div></body></html>"""
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(html)
    print("报表已生成:", REPORT)

if __name__ == "__main__":
    p = main()
    if not p:
        sys.exit(1)
    with open(os.path.join(OUT_DIR, "_etf_data.json"), "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
    render_html(p)
    print("数据更新完成, 最新数据日:", p["data_asof"], "共", len(p["dates"]), "个交易日")