"""
Daye Quarterly Theory - SMT alert bot (Python port of the Pine indicator).

Runs once per call (GitHub Actions runs it every 5 minutes):
  1. downloads candles, builds session-aligned bars (session day starts 18:00 New York)
  2. replays the SMT logic (Normal / Hidden SMT, FVG confirmation, fake SMT)
  3. sends NEW signals to Telegram (already sent ones are remembered in state.json)

Usage:
  python dqt_alert.py             normal run (sends alerts)
  python dqt_alert.py --test      send a test message to Telegram
  python dqt_alert.py --dry 24    print signals of the last 24 hours, send nothing

Groups of correlated assets (config.GROUPS): every asset of a group is used as the "chart" asset
against the other assets of the same group (same as the Pine indicator with compare symbols).
"""
import argparse
import json
import os
import sys
import time

import pandas as pd
import requests

import config as cfg

NY = "America/New_York"
CYC = ["Yearly", "Monthly", "Weekly", "Daily", "90min"]
TFN = ["D", "H4", "H1", "M15", "M5"]
DUR = [pd.Timedelta(days=1), pd.Timedelta(hours=4), pd.Timedelta(hours=1),
       pd.Timedelta(minutes=15), pd.Timedelta(minutes=5)]
SHIFT = pd.Timedelta(hours=6)          # session day starts 18:00 NY -> shift 6h to get the session date
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")

OHLC = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}


# =====================================================================
# Quarter logic (all times are naive New York wall-clock times)
# =====================================================================
def qidx(c, t):
    sh = t + SHIFT
    if c == 0:
        return (sh.month - 1) // 3
    if c == 1:
        # Monthly quarter = week, identified by its Monday (week starts Sunday 18:00 NY).
        # Monday on day 1-7 = Q1 ... 22-28 = Q4, Monday on day 29-31 = extra week (index 4)
        wd = sh.weekday()
        d0 = sh.normalize()
        mon = d0 + pd.Timedelta(days=1) if wd == 6 else d0 - pd.Timedelta(days=wd)
        return 4 if mon.day > 28 else (mon.day - 1) // 7
    if c == 2:
        wd = sh.weekday()
        return 0 if wd == 6 else min(wd, 3)
    m = ((t.hour - 18) % 24) * 60 + t.minute
    if c == 3:
        return m // 360
    return min(int((m % 90) // 22.5), 3)


# =====================================================================
# Data
# =====================================================================
def _to_ny_naive(idx):
    idx = pd.DatetimeIndex(idx)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    return idx.tz_convert(NY).tz_localize(None)


def fetch_yahoo(ticker, interval, period):
    import yfinance as yf
    last_err = None
    for _ in range(3):
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval,
                                           auto_adjust=False, actions=False)
            if df is not None and not df.empty:
                df = df[["Open", "High", "Low", "Close"]].dropna()
                df.index = _to_ny_naive(df.index)
                return df
        except Exception as e:  # noqa
            last_err = e
        time.sleep(2)
    print(f"  ! yahoo: no data for {ticker} {interval} ({last_err})")
    return None


def fetch_oanda(inst, gran, count):
    tok = os.environ.get("OANDA_TOKEN", "")
    host = os.environ.get("OANDA_HOST", "https://api-fxpractice.oanda.com")
    for _ in range(3):
        try:
            r = requests.get(f"{host}/v3/instruments/{inst}/candles",
                             params={"granularity": gran, "count": count, "price": "M"},
                             headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            r.raise_for_status()
            rows = [(c["time"], float(c["mid"]["o"]), float(c["mid"]["h"]),
                     float(c["mid"]["l"]), float(c["mid"]["c"]))
                    for c in r.json()["candles"] if c.get("complete")]
            if rows:
                df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close"])
                ts = pd.to_datetime(df.pop("t"), utc=True)
                df.index = _to_ny_naive(ts)
                return df
        except Exception as e:  # noqa
            print(f"  ! oanda {inst} {gran}: {e}")
        time.sleep(2)
    return None


_CACHE = {}


def load_symbol(asset, need):
    """returns {"1h": df, "15m": df, "5m": df} (only the needed ones), naive NY index"""
    prov = asset.get("provider", cfg.PROVIDER)
    out = {}
    for key in need:
        ck = (prov, asset.get(prov, asset["name"]), key)
        if ck not in _CACHE:
            if prov == "oanda":
                _CACHE[ck] = fetch_oanda(asset["oanda"], {"1h": "H1", "15m": "M15", "5m": "M5"}[key],
                                         {"1h": 5000, "15m": 1000, "5m": 1500}[key])
            else:
                _CACHE[ck] = fetch_yahoo(asset["yahoo"], {"1h": "1h", "15m": "15m", "5m": "5m"}[key],
                                         {"1h": "300d", "15m": "10d", "5m": "5d"}[key])
        out[key] = _CACHE[ck]
    return out


def agg(df, rule):
    d = df.copy()
    d.index = d.index + SHIFT
    r = d.resample(rule).agg(OHLC).dropna()
    r.index = r.index - SHIFT
    return r


def cycle_frame(raw, c, now):
    src = {0: "1h", 1: "1h", 2: "1h", 3: "15m", 4: "5m"}[c]
    df = raw.get(src)
    if df is None or df.empty:
        return None
    f = {0: lambda: agg(df, "1D"), 1: lambda: agg(df, "4h"), 2: lambda: agg(df, "1h"),
         3: lambda: df, 4: lambda: df}[c]()
    return f[f.index + DUR[c] <= now]          # completed bars only


# =====================================================================
# SMT state + logic (port of the Pine script)
# =====================================================================
class GS:
    """shared by all assets of a group: the current Weekly SMT (detected or confirmed)"""
    def __init__(self):
        self.wk = None              # dict(d, t, src, confirmed, first)
        self.events = []


class Ctx:
    def __init__(self, chart_name, group_name, gs):
        self.chart = chart_name
        self.group = group_name
        self.gs = gs
        self.arm = [0] * 5          # fake condition 1 state (index = lower cycle)


class SS:
    def __init__(self):
        self.tLast = None
        self.n = 0
        self.q = -1
        for f in ("aH aHt aL aLt bH bL paH paHt paL paLt pbH pbL "
                  "aBH aBHt aBL aBLt paBH paBHt paBL paBLt").split():
            setattr(self, f, None)
        self.aCL = self.bCL = self.aCH = self.bCH = False
        self.H = [None] * 3
        self.L = [None] * 3
        self.T = [None] * 3
        self.xL = [None] * 3
        self.xLt = [None] * 3
        self.xH = [None] * 3
        self.xHt = [None] * 3
        self.xBL = [None] * 3
        self.xBLt = [None] * 3
        self.xBH = [None] * 3
        self.xBHt = [None] * 3
        self.fsDir = 0
        self.fsName = ""
        self.cmpNm = ""
        self.pType = [0, 0]
        self.pN = [0, 0]
        self.pT = [None, None]
        self.pName = ["", ""]
        self.done = [False, False]


def week_start(t):
    """start of the session week (Sunday 18:00 NY) that contains t"""
    sh = t + SHIFT
    wd = sh.weekday()
    d0 = sh.normalize()
    mon = d0 + pd.Timedelta(days=1) if wd == 6 else d0 - pd.Timedelta(days=wd)
    return mon - SHIFT


def levels(s, d, kind, with_fvg):
    """(swept previous level, sweep extreme) of the SMT: Hidden = body, Normal = wick"""
    bod = kind == 2
    if d == 1:
        p1 = s.paBL if bod else s.paL
        if with_fvg:
            p2 = s.xBL[0] if bod else s.xL[0]
        else:
            p2 = s.aBL if bod else s.aL
    else:
        p1 = s.paBH if bod else s.paH
        if with_fvg:
            p2 = s.xBH[0] if bod else s.xH[0]
        else:
            p2 = s.aBH if bod else s.aH
    return p1, p2


def wk_update(ctx, s, d, tag, fake):
    """keep the group's current Weekly SMT (detected counts, FVG not needed)"""
    gs = ctx.gs
    wk = gs.wk
    t = s.T[2] + DUR[2]
    src = (id(s), d)
    cur = wk is not None and wk["t"] > week_start(s.T[2])
    if fake:
        if wk is not None and wk["src"] == src:
            gs.wk = None
        return
    if tag == "DET":
        if cur and wk["d"] == d:
            return                      # this week already has a Weekly SMT in this direction
        gs.wk = dict(d=d, t=t, src=src, confirmed=False, first=None)
    else:
        if wk is not None and wk["src"] == src:
            wk["confirmed"] = True
        elif cur and wk["d"] == d:
            wk["confirmed"] = True
        else:
            gs.wk = dict(d=d, t=t, src=src, confirmed=True, first=None)


def invalidate(ctx, s, c, d):
    """a pending SMT disappeared (both assets swept / closed beyond the level)"""
    wk = ctx.gs.wk
    if c == 2 and wk is not None and wk["src"] == (id(s), d) and not wk["confirmed"]:
        ctx.gs.wk = None


def emit(ctx, s, c, d, kind, tag, fake, with_fvg, det_t):
    p1, p2 = levels(s, d, kind, with_fvg)
    if c == 2:
        wk_update(ctx, s, d, tag, fake)
    # Daily SMT is "aligned" when it points the same way as this week's Weekly SMT.
    # "first" = the first Daily SMT (detection bar) after that Weekly SMT.
    aligned = False
    first = False
    wk = ctx.gs.wk
    if c == 3 and wk is not None and wk["d"] == d and wk["t"] > week_start(s.T[2]):
        aligned = True
        key = (det_t, d)
        if wk["first"] is None and not fake:
            wk["first"] = key
        first = wk["first"] == key
    ctx.gs.events.append(dict(group=ctx.group, chart=ctx.chart, tag=tag, c=c, d=d, kind=kind,
                              cmp=s.cmpNm, t3=s.T[2], avail=s.T[2] + DUR[c], fake=fake,
                              p1=p1, p2=p2, aligned=aligned, first=first))


def mark_smt(ctx, s, c, d, kind, nm, with_fvg, tag):
    f1 = cfg.FAKE_HTF and c > 0 and ctx.arm[c] == -d
    f2 = cfg.FAKE_FS and s.fsDir == -d and nm != s.fsName
    fake = f1 or f2
    if f1:
        ctx.arm[c] = 0
    if fake:
        s.fsDir = 0
    else:
        s.fsDir = d
        s.fsName = nm
        if c < 4:
            ctx.arm[c + 1] = d          # a normal SMT arms the cycle below
    emit(ctx, s, c, d, kind, tag, fake, with_fvg, s.pT[0 if d == 1 else 1])


def handle(ctx, s, c, d):
    k = 0 if d == 1 else 1
    swA = s.aL < s.paL if d == 1 else s.aH > s.paH
    swB = s.bL < s.pbL if d == 1 else s.bH > s.pbH
    clA = s.aCL if d == 1 else s.aCH
    clB = s.bCL if d == 1 else s.bCH
    pt = s.pType[k]
    if pt == 1 and swA and swB:
        s.pType[k] = 0
        invalidate(ctx, s, c, d)
    if pt == 2 and clA and clB:
        s.pType[k] = 0
        invalidate(ctx, s, c, d)
    if s.pType[k] == 0 and not s.done[k]:
        if cfg.SHOW_NORMAL and swA != swB:
            s.pType[k] = 1
            s.pN[k] = s.n
            s.pT[k] = s.T[2]
            s.pName[k] = s.cmpNm if swA else ctx.chart
            emit(ctx, s, c, d, 1, "DET", False, False, s.T[2])
        elif cfg.SHOW_HIDDEN and swA and swB and clA != clB:
            s.pType[k] = 2
            s.pN[k] = s.n
            s.pT[k] = s.T[2]
            s.pName[k] = s.cmpNm if clA else ctx.chart
            emit(ctx, s, c, d, 2, "DET", False, False, s.T[2])
    pt = s.pType[k]
    if pt != 0:
        fvg = s.n >= 3 and ((s.L[2] > s.H[0]) if d == 1 else (s.H[2] < s.L[0]))
        if not cfg.REQUIRE_FVG:
            mark_smt(ctx, s, c, d, pt, s.pName[k], False, "SMT")
            s.pType[k] = 0
            s.done[k] = True
        elif fvg and s.n - 2 >= s.pN[k]:
            mark_smt(ctx, s, c, d, pt, s.pName[k], True, "FVG")
            s.pType[k] = 0
            s.done[k] = True


def smt_step(ctx, s, c, aH, aL, aC, aO, aT, bH, bL, bC):
    s.tLast = aT
    bdH = max(aO, aC)
    bdL = min(aO, aC)
    s.n += 1
    s.H = s.H[1:] + [aH]
    s.L = s.L[1:] + [aL]
    s.T = s.T[1:] + [aT]
    qn = qidx(c, aT)
    if qn != s.q:
        if qn == 0 and c < 4:
            ctx.arm[c + 1] = 0          # new cycle: the bias it gave to the cycle below is cleared
        s.fsDir = 0
        s.fsName = ""
        if s.aH is not None:
            s.paH, s.paHt, s.paL, s.paLt = s.aH, s.aHt, s.aL, s.aLt
            s.pbH, s.pbL = s.bH, s.bL
            s.paBH, s.paBHt, s.paBL, s.paBLt = s.aBH, s.aBHt, s.aBL, s.aBLt
        s.aH, s.aHt, s.aL, s.aLt = aH, aT, aL, aT
        s.aBH, s.aBHt, s.aBL, s.aBLt = bdH, aT, bdL, aT
        s.bH, s.bL = bH, bL
        s.aCL = s.bCL = s.aCH = s.bCH = False
        s.q = qn
        s.pType = [0, 0]
        s.done = [False, False]
    else:
        if aH > s.aH:
            s.aH, s.aHt = aH, aT
        if aL < s.aL:
            s.aL, s.aLt = aL, aT
        if bdH > s.aBH:
            s.aBH, s.aBHt = bdH, aT
        if bdL < s.aBL:
            s.aBL, s.aBLt = bdL, aT
        s.bH = max(s.bH, bH)
        s.bL = min(s.bL, bL)
    s.xL = s.xL[1:] + [s.aL]
    s.xLt = s.xLt[1:] + [s.aLt]
    s.xH = s.xH[1:] + [s.aH]
    s.xHt = s.xHt[1:] + [s.aHt]
    s.xBL = s.xBL[1:] + [s.aBL]
    s.xBLt = s.xBLt[1:] + [s.aBLt]
    s.xBH = s.xBH[1:] + [s.aBH]
    s.xBHt = s.xBHt[1:] + [s.aBHt]
    if s.paH is not None:
        if aC < s.paL:
            s.aCL = True
        if bC < s.pbL:
            s.bCL = True
        if aC > s.paH:
            s.aCH = True
        if bC > s.pbH:
            s.bCH = True
        handle(ctx, s, c, 1)
        handle(ctx, s, c, -1)


# =====================================================================
# One chart set (chart asset + compare assets)
# =====================================================================
def active_cycles():
    return [c for c in range(5) if cfg.CYCLES.get(CYC[c]) and cfg.ALERT_CYCLES.get(CYC[c]) is not False]


def run_group(G, now, loader=load_symbol):
    assets = G["assets"]
    act = active_cycles()
    need = {{0: "1h", 1: "1h", 2: "1h", 3: "15m", 4: "5m"}[c] for c in act}
    print(f"[{G['name']}] loading data ...")
    raws = [loader(a, need) for a in assets]
    frames = {}
    for i in range(len(assets)):
        for c in act:
            frames[(i, c)] = cycle_frame(raws[i], c, now)

    tasks = []
    for p, pa in enumerate(assets):
        others = [q for q in range(len(assets)) if q != p]
        for c in act:
            A = frames[(p, c)]
            if A is None:
                print(f"  ! no data for {pa['name']} {TFN[c]}")
                continue
            for m, q in enumerate(others):
                B = frames[(q, c)]
                if B is None:
                    continue
                J = A.join(B, how="inner", rsuffix="_b")
                inv = bool(assets[q].get("inverse"))
                for r in J.itertuples():
                    if inv:
                        bH, bL, bC = -r.Low_b, -r.High_b, -r.Close_b
                    else:
                        bH, bL, bC = r.High_b, r.Low_b, r.Close_b
                    tasks.append((r.Index + DUR[c], c, p, m, q, r.Index,
                                  r.High, r.Low, r.Close, r.Open, bH, bL, bC))
    tasks.sort(key=lambda z: (z[0], z[1], z[2], z[3]))   # time order, higher cycle first

    gs = GS()
    ctxs = [Ctx(a["name"], G["name"], gs) for a in assets]
    states = {}
    for _, c, p, m, q, aT, aH, aL, aC, aO, bH, bL, bC in tasks:
        s = states.get((p, c, m))
        if s is None:
            s = states[(p, c, m)] = SS()
            s.cmpNm = assets[q]["name"]
        if s.tLast is None or aT > s.tLast:
            smt_step(ctxs[p], s, c, aH, aL, aC, aO, aT, bH, bL, bC)
    print(f"[{G['name']}] bars processed: {len(tasks)}, signals found (all history): {len(gs.events)}")
    return gs.events


# =====================================================================
# Messages, state, Telegram
# =====================================================================
def fmt(p):
    return "-" if p is None else f"{p:.2f}"


def group_events(events, now, max_age_min):
    groups = {}
    for e in events:
        if e["avail"] < now - pd.Timedelta(minutes=max_age_min):
            continue
        if e["tag"] == "DET" and not cfg.ALERT_DETECTED:
            continue
        if e["tag"] != "DET" and not cfg.ALERT_FVG:
            continue
        mode = cfg.ALERT_CYCLES.get(CYC[e["c"]], False)
        if mode is False:
            continue
        if mode == "aligned":
            if not e["aligned"] or e["fake"]:
                continue
            if cfg.DAILY_FIRST_ONLY and not e["first"]:
                continue
        if e["fake"] and not cfg.ALERT_FAKE:
            continue
        k = (e["group"], e["tag"], e["c"], e["d"], e["kind"], e["t3"], e["fake"])
        groups.setdefault(k, []).append(e)
    return sorted(groups.items(), key=lambda kv: (kv[1][0]["avail"], kv[1][0]["c"]))


def group_keys(k, evs):
    base = "|".join(str(x) for x in k)
    return [f"{base}|{e['chart']}|{e['cmp']}" for e in evs]


def build_message(k, evs):
    grp, tag, c, d, kind, t3, fake = k
    status = "Detected (waiting for FVG)" if tag == "DET" else ("SMT + FVG confirmed" if tag == "FVG" else "Confirmed")
    side = "Low" if d == 1 else "High"
    icon = "🟢" if d == 1 else "🔴"
    pairs = sorted({"/".join(sorted((e["chart"], e["cmp"]))) for e in evs})
    lines = [
        f"{icon} {'Bullish' if d == 1 else 'Bearish'} {'Normal' if kind == 1 else 'Hidden'} SMT"
        f"{' (FAKE)' if fake else ''} - {CYC[c]} ({TFN[c]})",
        f"{grp}: {', '.join(pairs)}",
        f"Status: {status}",
    ]
    if c == 3:
        lines.append("Aligned with Weekly SMT" + (" (first Daily SMT)" if cfg.DAILY_FIRST_ONLY else ""))
    seen = set()
    for e in evs:
        if e["chart"] in seen:
            continue
        seen.add(e["chart"])
        lines.append(f"{e['chart']} {side.lower()}: {fmt(e['p1'])} -> {fmt(e['p2'])}")
    lines.append(f"Bar: {t3.strftime('%m-%d %H:%M')} NY")
    return "\n".join(lines)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("sent", [])
            st.setdefault("seeded", False)
            return st
    except Exception:
        return {"sent": [], "seeded": False}


def save_state(st):
    st["sent"] = st["sent"][-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def send_telegram(text):
    tok = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        print("  ! TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=20)
        if r.status_code != 200:
            print(f"  ! telegram error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa
        print(f"  ! telegram error: {e}")
        return False


# =====================================================================
# Main
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a test message")
    ap.add_argument("--dry", type=float, default=None, metavar="HOURS",
                    help="print signals of the last N hours, send nothing")
    args = ap.parse_args()

    if args.test:
        ok = send_telegram("DQT bot test message: Telegram connection works.")
        print("sent" if ok else "FAILED")
        return 0 if ok else 1

    now = pd.Timestamp.now(tz=NY).tz_localize(None)
    events = []
    for G in cfg.GROUPS:
        events += run_group(G, now)

    if args.dry is not None:
        for k, evs in group_events(events, now, int(args.dry * 60)):
            print("-" * 40)
            print(build_message(k, evs))
        return 0

    st = load_state()
    sent = set(st["sent"])
    groups = group_events(events, now, cfg.MAX_AGE_MIN)

    if not st["seeded"]:
        # first run: remember what exists now, do not spam old signals
        for k, evs in groups:
            for x in group_keys(k, evs):
                if x not in sent:
                    sent.add(x)
                    st["sent"].append(x)
        st["seeded"] = True
        save_state(st)
        send_telegram("DQT bot is live. New SMT signals will arrive here.")
        print(f"first run: {len(groups)} existing signals remembered, nothing sent")
        return 0

    n = 0
    for k, evs in groups:
        keys = group_keys(k, evs)
        if all(x in sent for x in keys):
            continue
        if send_telegram(build_message(k, evs)):
            for x in keys:
                if x not in sent:
                    sent.add(x)
                    st["sent"].append(x)
            n += 1
            save_state(st)
    print(f"done: {n} new alert(s) sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
