#!/usr/bin/env python3
"""Market scanner (separate from gold bot): EUR/USD, WTI, BTC/USD.
Kourosh's system: 4H Ichimoku TK + cloud + RSI 55/45 (+chikou), 15m entry RSI vs SMA9 (+rsi_ext, atr).
  scanner.py --watch | --backtest | --test   (add --dry to print only)"""
import os, sys, json, time, datetime as dt, requests, pandas as pd, numpy as np, yfinance as yf
from zoneinfo import ZoneInfo
import matplotlib; matplotlib.use("Agg")
import mplfinance as mpf, matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
TZ, MT_TZ = ZoneInfo("Europe/Istanbul"), "Europe/Athens"
CHAT_IDS = [c.strip() for c in os.environ.get("SCANNER_TELEGRAM_CHAT_ID", "8561650547").split(",") if c.strip()]
CHAT_ID = CHAT_IDS[0]
TOKEN = os.environ.get("SCANNER_TELEGRAM_BOT_TOKEN", "")
STATE, TRADES, CALLS = [os.path.join(HERE, f) for f in ("state.json", "trades.json", "td_calls.json")]
# name, twelve data symbol (None = yfinance only), yfinance symbol, decimals, enabled
# BTC disabled: ~break-even after costs. WTI disabled: backtest negative (PF 0.85, -9.5R / 120 trades); WTI not on Twelve Data free tier
SYMBOLS = {
    "EURUSD": dict(fa="یورو/دلار", td="EUR/USD", yf="EURUSD=X", dec=5, on=True, fx=True),
    "WTI":    dict(fa="نفت WTI", td=None, yf="CL=F", dec=2, on=False),
    "BTCUSD": dict(fa="بیت‌کوین", td="BTC/USD", yf="BTC-USD", dec=1, on=False),
}
CROSS_LOOKBACK, SWING_BARS, ATR_BUF, MIN_ATR, MAX_ATR = 3, 20, 0.5, 1.0, 4.0
RSI_MAX_BUY, RSI_MIN_SELL, MIN_ATR_RATIO = 75, 25, 0.7
FILTERS = {"chikou", "rsi_ext", "atr"}
DRY = "--dry" in sys.argv

# ---------- data ----------
def _td(sym, interval, size):
    key = os.environ.get("SCANNER_TWELVEDATA_API_KEY")
    if not key or not sym: return None
    calls = [t for t in (json.load(open(CALLS)) if os.path.exists(CALLS) else []) if time.time() - t < 86400]
    if len(calls) >= 760: print("daily TD budget guard"); return None
    while sum(time.time() - t < 61 for t in calls) >= 7: time.sleep(5)
    try:
        j = requests.get("https://api.twelvedata.com/time_series", timeout=30, params=dict(
            symbol=sym, interval=interval, outputsize=size, timezone="UTC", apikey=key)).json()
        calls.append(time.time()); json.dump(calls, open(CALLS, "w"))
        if j.get("status") != "ok": raise RuntimeError(j.get("message"))
        d = pd.DataFrame(j["values"]); d.index = pd.to_datetime(d.pop("datetime")).dt.tz_localize("UTC")
        d = d.rename(columns=str.capitalize).astype(float).sort_index()
        if "Volume" not in d: d["Volume"] = 0.0
        return d[["Open","High","Low","Close","Volume"]]
    except Exception as e:
        print(f"twelvedata {sym} {interval} failed: {e}", file=sys.stderr); return None

def _yf(sym, interval, period):
    d = yf.download(sym, interval=interval, period=period, progress=False, auto_adjust=False)
    if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
    d = d[["Open","High","Low","Close","Volume"]].dropna()
    d.index = (d.index.tz_localize("UTC") if d.index.tz is None else d.index.tz_convert("UTC"))
    return d

def to_h4(h1):
    x = h1.copy(); x.index = x.index.tz_convert(MT_TZ)
    key = x.index.map(lambda t: t.replace(hour=t.hour - t.hour % 4, minute=0, second=0, microsecond=0))
    g = x.groupby(key).agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    g.index = pd.DatetimeIndex(g.index).tz_convert("UTC"); g.index.name = None
    return g.sort_index()

def _drop_weekend(d):
    """Twelve Data returns synthetic flat FX bars on weekends; MT5 has none."""
    return d if d is None or not len(d) else d[[not fx_closed(t.to_pydatetime()) for t in d.index]]

def get_data(name):
    s = SYMBOLS[name]
    h1, m15 = _td(s["td"], "1h", 1200), _td(s["td"], "15min", 800)
    if s.get("fx"): h1, m15 = _drop_weekend(h1), _drop_weekend(m15)
    if h1 is not None and m15 is not None and len(h1) > 600 and len(m15) > 300:
        return to_h4(h1), m15, "Twelve Data"
    return to_h4(_yf(s["yf"], "1h", "180d")), _yf(s["yf"], "15m", "30d"), "Yahoo (تأخیر ~۱۵ دقیقه)"

# ---------- indicators / rules (copied from gold bot) ----------
def indicators(d):
    d = d.copy(); h, l, c = d.High, d.Low, d.Close
    mid = lambda n: (h.rolling(n).max() + l.rolling(n).min()) / 2
    d["tenkan"], d["kijun"] = mid(9), mid(26)
    d["spanA"], d["spanB"] = ((d.tenkan + d.kijun) / 2).shift(26), mid(52).shift(26)
    delta = c.diff(); up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn); d["rsi_ma"] = d.rsi.rolling(9).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    d["atr_ratio"] = d.atr / d.atr.rolling(200, min_periods=50).median()
    top, bot = d[["spanA","spanB"]].max(axis=1), d[["spanA","spanB"]].min(axis=1)
    d["tk"] = np.sign(d.tenkan - d.kijun)
    d["cloud"] = np.where(c > top, 1, np.where(c < bot, -1, 0))
    d["chikou"] = np.where(c > h.shift(26), 1, np.where(c < l.shift(26), -1, 0))
    d["mapos"] = np.sign(d.rsi - d.rsi_ma)
    d["cross_id"] = (d.tk != d.tk.shift()).cumsum()
    return d

def checks(r, tf, side):
    b = side > 0
    cl = {1: "بالای ابر", -1: "زیر ابر"}.get(int(r.cloud), "داخل ابر")
    tkl = "تنکن بالای کیجون" if r.tk > 0 else ("تنکن زیر کیجون" if r.tk < 0 else "تنکن روی کیجون")
    rok = r.rsi > 55 if b else r.rsi < 45
    c = [(tkl + (f" (لازم: {'بالای' if b else 'زیر'})" if r.tk != side else ""), r.tk == side),
         ("قیمت " + cl + (f" (لازم: {'بالای' if b else 'زیر'} ابر)" if r.cloud != side else ""), r.cloud == side),
         (f"RSI {r.rsi:.1f}" + (f" (لازم: {'بالای ۵۵' if b else 'زیر ۴۵'})" if not rok else f" {'بالای ۵۵' if b else 'زیر ۴۵'}"), rok)]
    if tf == "15m": c.append((f"RSI {'بالای' if b else 'زیر'} مووینگ ۹", r.mapos == side))
    if tf == "4h" and "chikou" in FILTERS:
        c.append(("چیکو " + ("تأیید می‌کنه" if r.chikou == side else "تأیید نمی‌کنه"), r.chikou == side))
    if tf == "15m":
        if "rsi_ext" in FILTERS: c.append(("RSI اشباع نیست", r.rsi < RSI_MAX_BUY if b else r.rsi > RSI_MIN_SELL))
        if "atr" in FILTERS: c.append(("نوسان کافی (ATR)", r.atr_ratio >= MIN_ATR_RATIO))
    return c

def direction(r, tf):
    for side in (1, -1):
        if all(ok for _, ok in checks(r, tf, side)): return side
    return 0

def levels(d15, side):
    r = d15.iloc[-1]; atr = r.atr; e = r.Close; w = d15.tail(SWING_BARS)
    if side > 0:
        risk = min(max(e - (min(w.Low.min(), r.kijun) - ATR_BUF * atr), MIN_ATR * atr), MAX_ATR * atr)
        return dict(entry=e, sl=e - risk, tp1=e + risk, tp2=e + 2 * risk)
    risk = min(max((max(w.High.max(), r.kijun) + ATR_BUF * atr) - e, MIN_ATR * atr), MAX_ATR * atr)
    return dict(entry=e, sl=e + risk, tp1=e - risk, tp2=e - 2 * risk)

def recent(s, side, n=CROSS_LOOKBACK):
    s = s.dropna(); return any(s.iloc[-i] == side and s.iloc[-i-1] != side for i in range(1, n + 1) if len(s) > i)

# ---------- chart / telegram ----------
def plot(name, d4, d15, lv, path):
    fig = plt.figure(figsize=(13, 11)); g = fig.add_gridspec(4, 1, height_ratios=[3, 1, 3, 1], hspace=0.35)
    for k, (d, t) in enumerate([(d4.tail(120), f"{name} 4H"), (d15.tail(160), f"{name} 15m")]):
        ax, axr = fig.add_subplot(g[2*k]), fig.add_subplot(g[2*k+1])
        aps = [mpf.make_addplot(d.tenkan, ax=ax, color="blue", width=1), mpf.make_addplot(d.kijun, ax=ax, color="red", width=1),
               mpf.make_addplot(d.spanA, ax=ax, color="green", width=0.7), mpf.make_addplot(d.spanB, ax=ax, color="orange", width=0.7),
               mpf.make_addplot(d.rsi, ax=axr, color="purple", ylabel="RSI"), mpf.make_addplot(d.rsi_ma, ax=axr, color="goldenrod", width=1)]
        mpf.plot(d, type="candle", ax=ax, addplot=aps, style="yahoo", xrotation=0)
        x = np.arange(len(d)); a, b = d.spanA.values, d.spanB.values
        ax.fill_between(x, a, b, where=a >= b, color="green", alpha=0.15); ax.fill_between(x, a, b, where=a < b, color="red", alpha=0.15)
        axr.axhline(55, color="green", ls="--", lw=0.8); axr.axhline(45, color="red", ls="--", lw=0.8); axr.set_ylim(0, 100)
        if lv:
            for key, col in [("entry", "black"), ("sl", "red"), ("tp1", "green"), ("tp2", "darkgreen")]:
                ax.axhline(lv[key], color=col, ls=":", lw=1.2)
        ax.set_title(t)
    fig.savefig(path, dpi=100, bbox_inches="tight"); plt.close(fig)

def tg(method, **kw):
    if DRY: print(f"[dry] {method}: {kw.get('data', {}).get('text', kw.get('data', {}).get('caption', ''))}"); return
    j = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", timeout=60, **kw).json()
    if not j.get("ok"): raise RuntimeError(f"Telegram {method} failed: {j.get('description')}")
KB = json.dumps({"keyboard": [[{"text": "🔍 بررسی کن"}]], "resize_keyboard": True, "is_persistent": True})
TARGETS = CHAT_IDS  # reply mode narrows this to the asking chat
def _each(fn):
    for i, c in enumerate(TARGETS):
        try: fn(c)
        except Exception as e:
            if i == 0: raise
            print(f"extra chat send failed: {e}")
def say(t): _each(lambda c: tg("sendMessage", data={"chat_id": c, "text": t, "parse_mode": "HTML", "reply_markup": KB}))
def photo(p, cap):
    if DRY: print("[dry] photo", cap, TARGETS); return
    def f(c):
        with open(p, "rb") as fh: tg("sendPhoto", data={"chat_id": c, "caption": cap}, files={"photo": fh})
    _each(f)

def load(p, d): return json.load(open(p)) if os.path.exists(p) else d
def save(p, v):
    if not DRY: json.dump(v, open(p, "w"), ensure_ascii=False, indent=1, default=str)

# ---------- live ----------
def analyze(name):
    h4, m15, src = get_data(name); d4, d15 = indicators(h4), indicators(m15)
    dir4, dir15 = direction(d4.iloc[-1], "4h"), direction(d15.iloc[-1], "15m")
    side = dir4 if dir4 and dir15 == dir4 else 0
    age = (pd.Timestamp.now(tz="UTC") - d15.index[-1]).total_seconds()
    return dict(name=name, d4=d4, d15=d15, side=side, dir4=dir4, src=src, age=age,
                lv=levels(d15, side) if side else None, key=f"{side}|{d15.index[d15.cross_id == d15.cross_id.iloc[-1]][0]}")

def signal_msg(a):
    s = SYMBOLS[a["name"]]; f = lambda v: f"{v:.{s['dec']}f}"; lv = a["lv"]; side = a["side"]
    lines = [f"📡 <b>اسکنر بازار — {s['fa']}</b> ({a['src']})",
             ("\u200f🟢 <b>BUY</b>" if side > 0 else "\u200f🔴 <b>SELL</b>") + " — همهٔ شرط‌های ۴ ساعته و ۱۵ دقیقه تأیید شد.", ""]
    for tf, d, t in (("4h", a["d4"], "📊 ۴ ساعته"), ("15m", a["d15"], "⏱ ۱۵ دقیقه")):
        lines.append(f"<b>{t}</b>"); lines += [f"• {l} {'✅' if ok else '❌'}" for l, ok in checks(d.iloc[-1], tf, side)]
    lines += ["", f"🎯 <b>ورود:</b> {f(lv['entry'])}", f"🛑 <b>حد ضرر:</b> {f(lv['sl'])}",
              f"✅ <b>تارگت ۱ (1R):</b> {f(lv['tp1'])}", f"✅ <b>تارگت ۲ (2R):</b> {f(lv['tp2'])}"]
    return "\n".join(lines)

def track(name, d15, trades):
    s0 = SYMBOLS[name]; f = lambda v: f"{v:.{s0['dec']}f}"
    for t in trades:
        if t["sym"] != name or t["status"] == "closed": continue
        s = t["side"]; bars = d15[d15.index > pd.Timestamp(t.get("last_checked") or t["bar"])]
        nm = f"{'BUY' if s > 0 else 'SELL'} {s0['fa']}"
        for ts, b in bars.iterrows():
            stop = t["entry"] if t["tp1_hit"] else t["sl"]
            if (s > 0 and b.Low <= stop) or (s < 0 and b.High >= stop):
                t.update(status="closed", R=0.5 if t["tp1_hit"] else -1.0)
                say(f"{'⚪️' if t['tp1_hit'] else '🛑'} سیگنال {nm} از {f(t['entry'])}: " + ("بعد از تارگت ۱ به ورود برگشت (+0.5R)." if t["tp1_hit"] else f"حد ضرر {f(t['sl'])} خورد (-1R).")); break
            if not t["tp1_hit"] and ((s > 0 and b.High >= t["tp1"]) or (s < 0 and b.Low <= t["tp1"])):
                t["tp1_hit"] = True; say(f"✅ سیگنال {nm}: <b>تارگت ۱ ({f(t['tp1'])}) خورد</b>. نصف رو ببند و حد ضرر رو ببر روی ورود.")
            if t["tp1_hit"] and ((s > 0 and b.High >= t["tp2"]) or (s < 0 and b.Low <= t["tp2"])):
                t.update(status="closed", R=1.5); say(f"🏆 سیگنال {nm}: <b>تارگت ۲ ({f(t['tp2'])}) خورد</b> (+1.5R در کل)."); break
            if ts + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz="UTC"): t["last_checked"] = str(ts)  # forming bar re-checked next run

def fx_closed(now=None):
    """FX weekend: Fri 17:00 -> Sun 17:00 New York (DST-safe)."""
    t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    return t.weekday() == 5 or (t.weekday() == 4 and t.hour >= 17) or (t.weekday() == 6 and t.hour < 17)

def watch():
    st, trades = load(STATE, {}), load(TRADES, [])
    for name, s in SYMBOLS.items():
        if not s["on"]: continue
        if s.get("fx") and fx_closed(): print(f"{name}: forex weekend, skip"); continue
        try:
            a = analyze(name); track(name, a["d15"], trades)
            print(f"{name}: side={a['side']} dir4={a['dir4']} age={a['age']:.0f}s src={a['src']}")
            if a["age"] > 3600: continue
            key = a["key"] if a["side"] else "none"
            if key == st.get(name): continue
            st[name] = key
            if not a["side"]: continue
            if any(t["sym"] == name and t["status"] != "closed" for t in trades): print(f"{name}: signal skipped, trade still open"); continue
            p = os.path.join(HERE, f"chart_{name}.png"); plot(name, a["d4"], a["d15"], a["lv"], p)
            photo(p, f"چارت {s['fa']}: ۴ ساعته و ۱۵ دقیقه"); say(signal_msg(a))
            lv = a["lv"]; trades.append(dict(sym=name, side=a["side"], time=str(dt.datetime.now(TZ)), bar=str(a["d15"].index[-1]),
                                             status="open", tp1_hit=False, **{k: float(v) for k, v in lv.items()}))
        except Exception as e: print(f"{name} failed: {e}")
    save(STATE, st); save(TRADES, trades)

def test():
    lines = ["📡 <b>اسکنر بازار روشن شد</b> ✅", "این ربات جدا از ربات طلاست و فقط این نمادها رو با سیستم خودت (ایچیموکو + RSI روی ۴ ساعته، ورود ۱۵ دقیقه) اسکن می‌کنه:", ""]
    for name, s in SYMBOLS.items():
        if not s["on"]: continue
        a = analyze(name); r = a["d15"].iloc[-1]
        lines.append(f"• <b>{s['fa']}</b>: {r.Close:.{s['dec']}f} | روند ۴ساعته: {({1:'صعودی 📈',-1:'نزولی 📉',0:'خنثی ⏸'})[a['dir4']]}")
    lines += ["", "هر ۱۵ دقیقه چک می‌کنم و فقط وقتی همهٔ شرط‌ها جور شد سیگنال با ورود، حد ضرر و تارگت می‌فرستم."]
    p = os.path.join(HERE, "chart_test.png"); plot(name, a["d4"], a["d15"], None, p)
    photo(p, f"چارت نمونه: {s['fa']}"); say("\n".join(lines)); print("TEST SENT")

def reply(name="EURUSD"):
    """On-demand check (Telegram button): current analysis + open trade status, works when market closed."""
    s = SYMBOLS[name]; f = lambda v: f"{v:.{s['dec']}f}"
    a = analyze(name); d4, d15 = a["d4"], a["d15"]; r4, r15 = d4.iloc[-1], d15.iloc[-1]
    lean = a["dir4"] or int(r4.tk) or 1
    last = d15.index[-1].tz_convert(TZ)
    L = []
    if (s.get("fx") and fx_closed()) or a["age"] > 3600:
        L += ["⛔ <b>بازار تعطیله، این تحلیل با آخرین قیمت جمعه‌ست</b>", ""]
    L += [f"🔍 <b>بررسی {s['fa']}</b> ({a['src']})", f"💰 قیمت: <b>{f(r15.Close)}</b>  (آخرین کندل: {last:%m-%d %H:%M} استانبول)"]
    L += ["", f"<b>📊 روند ۴ ساعته:</b> {({1:'صعودی 📈',-1:'نزولی 📉',0:'خنثی ⏸'})[a['dir4']]}"]
    L += [f"• {l} {'✅' if ok else '❌'}" for l, ok in checks(r4, "4h", lean)]
    L += ["", f"<b>⏱ شرط‌های ورود ۱۵ دقیقه ({'BUY' if lean > 0 else 'SELL'}):</b>"]
    L += [f"• {l} {'✅' if ok else '❌'}" for l, ok in checks(r15, "15m", lean)]
    L.append(f"• RSI: {r15.rsi:.1f} | مووینگ ۹: {r15.rsi_ma:.1f}")
    op = [t for t in load(TRADES, []) if t["sym"] == name and t["status"] != "closed"]
    L.append("")
    if op:
        t = op[-1]; pnl = (r15.Close - t["entry"]) * t["side"]
        L.append(f"📌 <b>معاملهٔ باز:</b> {'BUY' if t['side'] > 0 else 'SELL'} از {f(t['entry'])} | حد ضرر {f(t['entry'] if t['tp1_hit'] else t['sl'])} | "
                 f"تارگت ۱ {f(t['tp1'])}{' ✅' if t['tp1_hit'] else ''} | تارگت ۲ {f(t['tp2'])}\n   الان {'در سود' if pnl > 0 else 'در ضرر'}: {pnl / abs(t['entry'] - t['sl']):+.2f}R")
    else: L.append("📌 معاملهٔ بازی نداریم.")
    if a["side"]: L.append(f"\n✅ <b>الان همهٔ شرط‌ها {'BUY' if a['side'] > 0 else 'SELL'} جوره</b> (سیگنال رسمی با ورود/حد ضرر از واچر میاد).")
    elif a["dir4"]: L.append(f"\n⏳ روند ۴ ساعته تأیید شده؛ منتظر ۱۵ دقیقه‌ام.")
    else: L.append("\n⏳ روند ۴ ساعته هنوز تأیید نشده؛ فعلاً صبر.")
    p = os.path.join(HERE, "chart_reply.png"); plot(name, d4, d15, None, p)
    photo(p, f"چارت {s['fa']}: ۴ ساعته و ۱۵ دقیقه"); say("\n".join(L)); print("REPLY SENT")

# ---------- backtest ----------
def backtest(name):
    s = SYMBOLS[name]
    h4 = indicators(to_h4(_yf(s["yf"], "1h", "730d"))); d15 = indicators(_yf(s["yf"], "15m", "60d"))
    d4 = h4.copy(); d4["dir"] = [direction(r, "4h") for r in d4.itertuples()]; d4.index = d4.index + pd.Timedelta(hours=4)
    d4.index = d4.index.as_unit("ns"); d15.index = d15.index.as_unit("ns")
    m = d15.copy(); m["dir15"] = [direction(r, "15m") for r in m.itertuples()]
    m = pd.merge_asof(m.sort_index(), d4[["dir"]].sort_index(), left_index=True, right_index=True)
    m["side"] = np.where((m.dir == m.dir15) & (m.dir != 0), m.dir, 0)
    rows = list(m.itertuples()); res = []; prev = None
    for i, r in enumerate(rows):
        key = (r.side, r.cross_id) if r.side else None
        if i < 200 or key == prev: prev = key; continue
        prev = key
        if not key: continue
        lv = levels(m.iloc[: i + 1], r.side); sd = r.side; tp1 = False; out = None
        for b in rows[i + 1:]:
            stop = lv["entry"] if tp1 else lv["sl"]
            if (sd > 0 and b.Low <= stop) or (sd < 0 and b.High >= stop): out = 0.5 if tp1 else -1.0; break
            if not tp1 and ((sd > 0 and b.High >= lv["tp1"]) or (sd < 0 and b.Low <= lv["tp1"])): tp1 = True
            if tp1 and ((sd > 0 and b.High >= lv["tp2"]) or (sd < 0 and b.Low <= lv["tp2"])): out = 1.5; break
        if out is not None: res.append(out)
    r = pd.Series(res, dtype=float); w, l = r[r > 0].sum(), -r[r < 0].sum()
    return dict(sym=name, days=(d15.index[-1] - d15.index[0]).days, trades=len(r), win=(r > 0).mean() * 100 if len(r) else 0,
                avgR=r.mean() if len(r) else 0, totR=r.sum(), PF=w / l if l else float("inf"))

if __name__ == "__main__":
    if "--backtest" in sys.argv:
        for n in SYMBOLS: print(backtest(n))
    elif "--test" in sys.argv: test()
    elif "--reply" in sys.argv:
        ask = os.environ.get("TG_CHAT", "").strip()
        TARGETS = [ask if ask in CHAT_IDS else CHAT_ID]
        reply()
    else: watch()
