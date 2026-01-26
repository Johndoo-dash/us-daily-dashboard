import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import feedparser
from bs4 import BeautifulSoup
from finance_calendars import finance_calendars as fc

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "latest.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

KST = timezone(timedelta(hours=9))

MY_TICKERS = ["NE","RXRX","BLDP","BMNR","NVDA","TSLA","AI","GGLL","QQQM","VRTL","CEVA","CCS"]

KO_NAME = {
    "NE":"노블 코퍼레이션",
    "RXRX":"리커전 파마",
    "BLDP":"발라드 파워",
    "BMNR":"비트마인드(가칭)",
    "NVDA":"엔비디아",
    "TSLA":"테슬라",
    "AI":"C3.ai",
    "GGLL":"그래닛셰어즈 2x 롱 NVDA(ETF)",
    "QQQM":"인베스코 나스닥100 미니(ETF)",
    "VRTL":"버추얼트…(가칭)",
    "CEVA":"세바",
    "CCS":"센추리 커뮤니티즈",
}

SECTOR_ETFS = [
    ("XLK", "기술"),
    ("XLF", "금융"),
    ("XLY", "경기소비재"),
    ("XLP", "필수소비재"),
    ("XLE", "에너지"),
    ("XLV", "헬스케어"),
    ("XLI", "산업재"),
    ("XLB", "소재"),
    ("XLU", "유틸리티"),
    ("XLRE", "부동산"),
    ("XLC", "커뮤니케이션"),
]

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def pct_change(last, prev):
    if last is None or prev is None or prev == 0:
        return 0.0
    return (last / prev - 1.0) * 100.0

def stooq_csv(symbol: str) -> str:
    # Stooq CSV endpoint (daily)
    return f"https://stooq.com/q/d/l/?s={symbol}&i=d"

def fetch_stooq_close_series(symbol: str, limit: int = 35):
    """
    returns (labels(list[str]), closes(list[float]))
    """
    url = stooq_csv(symbol.lower())
    r = requests.get(url, headers=UA_HEADERS, timeout=25)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 3:
        return [], []
    # header: Date,Open,High,Low,Close,Volume
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        dt = parts[0].strip()
        close = safe_float(parts[4])
        if close is None:
            continue
        rows.append((dt, close))
    rows = rows[-limit:]
    labels = [d for d, _ in rows]
    closes = [c for _, c in rows]
    return labels, closes

def fetch_fred_dgs10(limit: int = 35):
    # no key needed
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
    r = requests.get(url, headers=UA_HEADERS, timeout=25)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    rows = []
    for line in lines[1:]:
        d, v = line.split(",")
        val = safe_float(v)  # already percent
        if val is None:
            continue
        rows.append((d.strip(), val))
    rows = rows[-limit:]
    labels = [d for d, _ in rows]
    vals = [v for _, v in rows]
    return labels, vals

def google_news_rss(query: str, max_items: int = 10):
    # Google News RSS
    # NOTE: query should be url-escaped minimally (spaces -> +)
    q = query.replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries[:max_items]:
        title = getattr(e, "title", "").strip()
        # source often embedded like " - Reuters"
        source = ""
        if " - " in title:
            source = title.split(" - ")[-1].strip()
        summary = re.sub(r"\s+", " ", re.sub("<[^>]+>", "", getattr(e, "summary", "")).strip())
        items.append({
            "title": title,
            "why": summary[:140] + ("…" if len(summary) > 140 else ""),
            "source": source or "Google News",
            "star": False,
        })
    # de-dup by title
    seen = set()
    out = []
    for it in items:
        key = it["title"].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out[:5]

def fetch_fed_schedule(limit: int = 6):
    # Use Fed "Monetary Policy" upcoming dates (contains FOMC meeting + minutes)
    url = "https://www.federalreserve.gov/monetarypolicy.htm"
    r = requests.get(url, headers=UA_HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # crude parse: find "Upcoming Dates" block
    try:
        idx = lines.index("Upcoming Dates")
    except ValueError:
        return []
    block = lines[idx: idx + 80]
    events = []
    # pattern: "Jan. 27-28 FOMC Meeting" etc
    for ln in block:
        if len(events) >= limit:
            break
        if "FOMC" in ln or "Minutes" in ln:
            events.append(ln)
    # format into UI rows
    out = []
    for ev in events[:limit]:
        # split into date + title
        m = re.match(r"^([A-Za-z]{3,4}\.\s*\d{1,2}(?:-\d{1,2})?)\s+(.*)$", ev)
        if m:
            dpart = m.group(1)
            title = m.group(2)
            out.append({"time": dpart, "title": title, "note": "Fed(공식 캘린더)"})
        else:
            out.append({"time": "-", "title": ev, "note": "Fed(공식 캘린더)"})
    return out

def build_earnings_next_7days(myset):
    items = []
    today = datetime.now(KST).date()
    for i in range(0, 7):
        d = today + timedelta(days=i)
        try:
            rows = fc.get_earnings_by_date(datetime(d.year, d.month, d.day, 0, 0))
        except Exception:
            rows = []
        # rows: list[dict]
        for r in rows:
            sym = str(r.get("symbol") or r.get("Symbol") or "").upper().strip()
            name = str(r.get("name") or r.get("Name") or "").strip()
            timing = str(r.get("time") or r.get("Time") or r.get("timing") or "").strip()  # BMO/AMC sometimes
            if sym in myset:
                when = f"{d.isoformat()} {timing}".strip()
                items.append({
                    "when": when,
                    "symbol": sym,
                    "name": name or KO_NAME.get(sym, ""),
                    "note": "실적 캘린더(Nasdaq)",
                })
    return items[:12]

def main():
    now_kst = datetime.now(KST)
    updated_at = now_kst.strftime("%Y-%m-%d %H:%M KST")

    # Indices / VIX
    spx_labels, spx = fetch_stooq_close_series("^spx", 35)
    ndq_labels, ixic = fetch_stooq_close_series("^ndq", 35)
    dji_labels, dji = fetch_stooq_close_series("^dji", 35)
    vix_labels, vix = fetch_stooq_close_series("vi.f", 35)

    def last_prev(arr):
        if len(arr) < 2:
            return None, None
        return arr[-1], arr[-2]

    spx_last, spx_prev = last_prev(spx)
    ixic_last, ixic_prev = last_prev(ixic)
    dji_last, dji_prev = last_prev(dji)
    vix_last, vix_prev = last_prev(vix)

    overnight_kpis = [
        {"icon":"📈","label":"S&P500","valueText": f"{spx_last:,.2f}" if spx_last else "-", "desc":"대표 지수", "changePct": round(pct_change(spx_last, spx_prev), 2) if spx_last else 0},
        {"icon":"📈","label":"나스닥","valueText": f"{ixic_last:,.2f}" if ixic_last else "-", "desc":"기술주 비중", "changePct": round(pct_change(ixic_last, ixic_prev), 2) if ixic_last else 0},
        {"icon":"📈","label":"다우","valueText": f"{dji_last:,.2f}" if dji_last else "-", "desc":"대형 가치주", "changePct": round(pct_change(dji_last, dji_prev), 2) if dji_last else 0},
        {"icon":"😱","label":"VIX","valueText": f"{vix_last:,.2f}" if vix_last else "-", "desc":"불안하면 ↑", "changePct": round(pct_change(vix_last, vix_prev), 2) if vix_last else 0},
    ]

    # Macro: US10Y (FRED), DXY (DX.F), WTI (CL.F)
    us10y_labels, us10y = fetch_fred_dgs10(35)
    dxy_labels, dxy = fetch_stooq_close_series("dx.f", 35)
    wti_labels, wti = fetch_stooq_close_series("cl.f", 35)

    us10y_last, us10y_prev = last_prev(us10y)
    dxy_last, dxy_prev = last_prev(dxy)
    wti_last, wti_prev = last_prev(wti)

    macro_kpis = [
        {"icon":"🏦","label":"미국 10년 금리","valueText": f"{us10y_last:.2f}%" if us10y_last else "-", "desc":"FRED(DGS10)", "changePct": round(pct_change(us10y_last, us10y_prev), 2) if us10y_last else 0},
        {"icon":"💵","label":"달러값(DXY)","valueText": f"{dxy_last:.2f}" if dxy_last else "-", "desc":"Stooq(DX.F)", "changePct": round(pct_change(dxy_last, dxy_prev), 2) if dxy_last else 0},
        {"icon":"🛢️","label":"유가(WTI)","valueText": f"{wti_last:.2f}" if wti_last else "-", "desc":"Stooq(CL.F)", "changePct": round(pct_change(wti_last, wti_prev), 2) if wti_last else 0},
    ]

    # align macro labels by us10y labels for chart (simple approach)
    labels = us10y_labels[-30:] if us10y_labels else (spx_labels[-30:] if spx_labels else [])
    # build lookup dict for dxy/wti by date
    dxy_map = {d: v for d, v in zip(dxy_labels, dxy)}
    wti_map = {d: v for d, v in zip(wti_labels, wti)}
    us10y_map = {d: v for d, v in zip(us10y_labels, us10y)}
    macro_series = {
        "labels": labels,
        "us10y": [us10y_map.get(d) for d in labels],
        "dxy": [dxy_map.get(d) for d in labels],
        "wti": [wti_map.get(d) for d in labels],
    }

    # My stocks (Stooq: ticker.us)
    mystocks = []
    my_changes = []
    for t in MY_TICKERS:
        sym = t.upper()
        stooq_sym = f"{sym.lower()}.us"
        lbls, closes = fetch_stooq_close_series(stooq_sym, 3)
        last, prev = last_prev(closes)
        chg = round(pct_change(last, prev), 2) if last and prev else 0.0
        my_changes.append((sym, chg))
        last_txt = f"{last:.2f}" if last else "-"
        price_text = f"${last_txt} ({'↑' if chg>=0 else '↓'}{abs(chg):.2f}%)" if last else "-"
        mystocks.append({
            "symbol": sym,
            "name": KO_NAME.get(sym, ""),
            "koName": KO_NAME.get(sym, ""),
            "last": last_txt if last else "-",
            "changePct": chg,
            "priceText": price_text,
            "news": "",
            "nextEvent": "",
            "memo": "",
        })

    # Sectors from SPDR sector ETFs
    sectors = []
    for etf, ko in SECTOR_ETFS:
        lbls, closes = fetch_stooq_close_series(f"{etf.lower()}.us", 3)
        last, prev = last_prev(closes)
        chg = round(pct_change(last, prev), 2) if last and prev else 0.0
        sectors.append({"name": ko, "changePct": chg, "symbol": etf})

    # Earnings (next 7 days, my tickers only)
    myset = set(MY_TICKERS)
    upcoming = build_earnings_next_7days(myset)

    # Movers: use myStocks top/bottom as "급등락" (실적 섹션의 대체 카드)
    my_sorted = sorted(mystocks, key=lambda x: x.get("changePct", 0), reverse=True)
    movers = []
    for x in (my_sorted[:3] + my_sorted[-3:]):
        movers.append({
            "symbol": x["symbol"],
            "name": x.get("name",""),
            "changePct": x.get("changePct", 0),
            "why": "내 종목 기준 급등락(프록시) — 실적 연동 여부는 뉴스 확인",
        })

    # Schedule
    fed_events = fetch_fed_schedule(6)
    # econ: 넣을만한 무료 정식 캘린더가 빡세서, v1은 "내 종목 실적/주요 이벤트"로 채움
    econ_events = []
    for u in upcoming[:6]:
        econ_events.append({"time": u["when"].split()[0], "title": f"Earnings: {u['symbol']}", "note": u.get("name","")})
    if not econ_events:
        econ_events = []

    # News Top5 (market general + tickers mix)
    news = google_news_rss("US stock market futures S&P 500", 12)
    if len(news) < 5:
        extra = google_news_rss("Nasdaq earnings", 12)
        news = (news + extra)[:5]

    # Mood/Action simple rule
    spx_chg = pct_change(spx_last, spx_prev) if spx_last and spx_prev else 0
    vix_val = vix_last if vix_last else None
    if spx_chg > 0.5 and (vix_val is None or vix_val < 18):
        mood = {"value": "좋음", "reason": "지수 강세 + 변동성 낮은 편"}
        action = {"value": "매수(조금)", "note": "분할/소액 중심", "beginnerMemo": "급할수록 분할. 한 번에 올인 금지."}
    elif spx_chg < -0.5 and (vix_val is not None and vix_val > 20):
        mood = {"value": "나쁨", "reason": "지수 약세 + 변동성 상승"}
        action = {"value": "관망", "note": "리스크 관리 우선", "beginnerMemo": "손실 줄이는 날이 진짜 수익."}
    else:
        mood = {"value": "애매", "reason": "방향성 약함(보합/혼조)"}
        action = {"value": "관망", "note": "확실한 구간까지 기다리기", "beginnerMemo": "관망도 전략. 애매하면 쉬는 게 이김."}

    # One-line summary
    top = max(mystocks, key=lambda x: x.get("changePct", 0), default=None)
    bot = min(mystocks, key=lambda x: x.get("changePct", 0), default=None)
    one_line = (
        f"지수 {('상승' if spx_chg>=0 else '하락')}({spx_chg:+.2f}%), "
        f"10Y {pct_change(us10y_last, us10y_prev):+.2f}%, "
        f"DXY {pct_change(dxy_last, dxy_prev):+.2f}%, "
        f"WTI {pct_change(wti_last, wti_prev):+.2f}%. "
        f"내 종목 TOP: {top['symbol']}({top['changePct']:+.2f}%) / "
        f"BOT: {bot['symbol']}({bot['changePct']:+.2f}%)."
        if top and bot else "자동 업데이트: 지수/금리/달러/유가 + 내 종목 변동 반영"
    )

    # Risk
    max_abs = max((abs(x.get("changePct",0)) for x in mystocks), default=0)
    risk = {
        "speed": f"내 종목 최대 절대등락: {max_abs:.2f}%",
        "vol": f"VIX: {vix_last:.2f}" if vix_last else "VIX: -",
        "rule": f"오늘 액션: {action['value']} (무리 금지)",
    }

    # Overnight series (30)
    ov_labels = spx_labels[-30:] if spx_labels else []
    # align other indices by date
    ixic_map = {d: v for d, v in zip(ndq_labels, ixic)}
    dji_map = {d: v for d, v in zip(dji_labels, dji)}
    overnight_series = {
        "labels": ov_labels,
        "spx": [v for v in spx[-30:]] if spx else [],
        "ixic": [ixic_map.get(d) for d in ov_labels],
        "dji": [dji_map.get(d) for d in ov_labels],
    }

    payload = {
        "updatedAt": updated_at,
        "oneLine": one_line,
        "mood": mood,
        "action": action,
        "overnight": {
            "kpis": overnight_kpis,
            "bigFlowReason": "무료 데이터 기반 자동 생성(v1): 지수/변동성/금리/달러/유가 + 내 종목 급등락을 결합",
            "series": overnight_series,
        },
        "schedule": {"econ": econ_events, "fed": fed_events},
        "macro": {"kpis": macro_kpis, "series": macro_series},
        "newsTop5": news,
        "earnings": {"upcoming": upcoming, "movers": movers},
        "sectors": [{"name": x["name"], "changePct": x["changePct"]} for x in sectors],
        "myStocks": mystocks,
        "risk": risk,
        "todo3": [
            "내 종목 변동 상위/하위 3개만 따로 체크",
            "급등/급락 종목은 뉴스 확인 후 대응",
            "오늘은 ‘한 번만’ 매매 규칙 지키기"
        ],
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Wrote:", OUT)

if __name__ == "__main__":
    main()
