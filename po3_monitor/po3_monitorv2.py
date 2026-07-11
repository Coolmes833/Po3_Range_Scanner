#!/usr/bin/env python3
"""
Po3 / AMD monitor v4 — Binance USDT-M Futures, stdlib only (Python 3.6+).

v3 features (multi-TF retest alerts via Telegram/e-mail) PLUS:
  1. HTF bias filter    : 15m/30m/1h setups must align with 4h bias
                          (4h setups align with 1d). Disable: --no-htf
  2. Displacement score : strong-bodied reclaim candle earns bonus points
  3. OI confirmation    : Open Interest drop during the sweep = real stop-out,
                          earns bonus points. Disable: --no-oi
  4. Kill zone filter   : sweep must happen in London (07-10 UTC) or
                          NY (12:30-16 UTC) session. Disable: --no-killzone
  5. Outcome log        : every alert -> po3_alerts.csv; each cycle checks
                          whether target (-0.272 ext) or stop (sweep) was hit
                          first and marks WIN/LOSS.  Stats: --stats

Usage:
    python3 po3_monitor_v4.py --test-alert     # test Telegram/mail
    python3 po3_monitor_v4.py --once           # single scan (cron friendly)
    nohup python3 po3_monitor_v4.py > po3.log 2>&1 &   # run forever
    python3 po3_monitor_v4.py --stats          # win rate / avg R report
"""

import argparse
import csv
import json
import os
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

# =========================== CONFIG ===========================
CONFIG = {
    # --- Telegram (leave token empty to disable) ---
    "TELEGRAM_BOT_TOKEN": "",        # e.g. "123456789:AAH4x..."
    "TELEGRAM_CHAT_ID": "",          # e.g. "987654321"

    # --- E-mail via SMTP (set EMAIL_ENABLED True to enable) ---
    "EMAIL_ENABLED": False,
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": 587,
    "SMTP_USER": "",
    "SMTP_PASS": "",                 # Gmail: App Password
    "MAIL_TO": "",
}
# ==============================================================

BASE = "https://fapi.binance.com"
CTX = ssl.create_default_context()
TFS = ["15m", "30m", "1h", "4h"]
TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
HTF_OF = {"15m": "4h", "30m": "4h", "1h": "4h", "4h": "1d"}
# Kill zones in UTC decimal hours: London 07:00-10:00, NY 12:30-16:00
KILLZONES = [("London", 7.0, 10.0), ("NY", 12.5, 16.0)]

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "po3_alert_state.json")
LOG_FILE = os.path.join(HERE, "po3_alerts.csv")
CSV_FIELDS = ["alert_ts", "alert_time", "symbol", "tf", "side", "score",
              "range_low", "range_high", "sweep", "entry", "target",
              "flags", "status", "result", "r_multiple", "resolved_time"]


# ----------------------- HTTP helpers -----------------------
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10, context=CTX) as r:
        return json.loads(r.read().decode())


def get_symbols(min_quote_vol):
    tickers = http_get(BASE + "/fapi/v1/ticker/24hr")
    syms = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) >= min_quote_vol
    ]
    syms.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in syms]


def get_klines(symbol, interval, limit=120):
    url = "{}/fapi/v1/klines?symbol={}&interval={}&limit={}".format(
        BASE, symbol, interval, limit)
    return [
        {
            "t": int(k[0]) / 1000.0,
            "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]),
            "v": float(k[7]),
        }
        for k in http_get(url)
    ]


def get_oi_change_pct(symbol, tf, bars):
    """OI % change over the manipulation window. Negative = OI dropped."""
    url = ("{}/futures/data/openInterestHist?symbol={}&period={}&limit={}"
           .format(BASE, symbol, tf, min(bars + 2, 30)))
    data = http_get(url)
    if len(data) < 2:
        return None
    first = float(data[0]["sumOpenInterest"])
    last = float(data[-1]["sumOpenInterest"])
    if first <= 0:
        return None
    return (last - first) / first * 100.0


# ----------------------- Alert channels -----------------------
def send_telegram(text):
    tok = CONFIG["TELEGRAM_BOT_TOKEN"]
    if not tok:
        return False
    data = urllib.parse.urlencode({
        "chat_id": CONFIG["TELEGRAM_CHAT_ID"], "text": text}).encode()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot{}/sendMessage".format(tok), data=data)
        urllib.request.urlopen(req, timeout=10, context=CTX)
        return True
    except Exception as e:
        print("Telegram error:", e)
        return False


def send_email(subject, body):
    if not CONFIG["EMAIL_ENABLED"]:
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = CONFIG["SMTP_USER"]
        msg["To"] = CONFIG["MAIL_TO"]
        s = smtplib.SMTP(CONFIG["SMTP_HOST"], CONFIG["SMTP_PORT"], timeout=15)
        s.starttls()
        s.login(CONFIG["SMTP_USER"], CONFIG["SMTP_PASS"])
        s.sendmail(CONFIG["SMTP_USER"], [CONFIG["MAIL_TO"]], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        print("Mail error:", e)
        return False


def alert(subject, body):
    ok = send_telegram(subject + "\n" + body)
    ok = send_email(subject, body) or ok
    if not ok:
        print("!! No alert channel configured/working. Message was:\n", body)


# ----------------------- Filters -----------------------
def in_killzone(epoch_ts):
    """Return kill zone name if UTC time falls in London/NY window, else None."""
    tm = time.gmtime(epoch_ts)
    h = tm.tm_hour + tm.tm_min / 60.0
    for name, start, end in KILLZONES:
        if start <= h < end:
            return name
    return None


def htf_bias(htf_candles, lookback=40):
    """'long' if close above HTF range mid, 'short' if below."""
    acc = htf_candles[-lookback:]
    rh = max(c["h"] for c in acc)
    rl = min(c["l"] for c in acc)
    mid = (rh + rl) / 2
    return "long" if htf_candles[-1]["c"] > mid else "short"


# ----------------------- Po3 analysis -----------------------
def analyze(candles, side, args):
    """Returns setup dict when RETEST condition is met, else None."""
    acc_len, manip_len = args.acc_len, args.manip_len
    if len(candles) < acc_len + manip_len + 2:
        return None

    acc = candles[-(acc_len + manip_len):-manip_len]
    recent = candles[-manip_len:]
    last = candles[-1]

    rh = max(c["h"] for c in acc)
    rl = min(c["l"] for c in acc)
    width = rh - rl
    if width <= 0:
        return None

    avg_body = sum(abs(c["c"] - c["o"]) for c in acc) / len(acc)
    if width > args.width_mult * avg_body:
        return None
    if abs(acc[-1]["c"] - acc[0]["o"]) > args.drift * width:
        return None

    tol = args.retest_tol * width
    if side == "long":
        sweep_candle = min(recent, key=lambda c: c["l"])
        sweep = sweep_candle["l"]
        if sweep >= rl:
            return None
        reclaims = [c for c in recent if c["c"] > rl]
        if not reclaims:
            return None
        if not (last["l"] <= rl + tol and rl < last["c"] < rh):
            return None
        sweep_depth = (rl - sweep) / width
        pos = (last["c"] - rl) / width
        target = rh + 0.272 * width
        stop = sweep
    else:
        sweep_candle = max(recent, key=lambda c: c["h"])
        sweep = sweep_candle["h"]
        if sweep <= rh:
            return None
        reclaims = [c for c in recent if c["c"] < rh]
        if not reclaims:
            return None
        if not (last["h"] >= rh - tol and rl < last["c"] < rh):
            return None
        sweep_depth = (sweep - rh) / width
        pos = (rh - last["c"]) / width
        target = rl - 0.272 * width
        stop = sweep

    # ---- scoring ----
    flags = []
    depth_score = 1.0 - abs(sweep_depth - 0.35) if sweep_depth < 1.0 else 0.0
    pos_score = 1.0 - abs(pos - 0.35)
    score = 2.0 * depth_score + 1.5 * pos_score

    # displacement: strongest reclaim candle body vs accumulation avg body
    best_body = max(abs(c["c"] - c["o"]) for c in reclaims)
    if avg_body > 0 and best_body >= args.disp_mult * avg_body:
        score += 1.0
        flags.append("DISP")

    return {
        "side": side.upper(),
        "score": score,
        "range_low": rl, "range_high": rh,
        "sweep": sweep, "sweep_ts": sweep_candle["t"],
        "entry": last["c"], "target": round(target, 6), "stop": stop,
        "flags": flags,
    }


# ----------------------- State / CSV log -----------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("State save error:", e)


def load_log():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return list(csv.DictReader(f))


def save_log(rows):
    with open(LOG_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def log_alert(res, sym, tf, now):
    rows = load_log()
    rows.append({
        "alert_ts": int(now),
        "alert_time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
        "symbol": sym, "tf": tf, "side": res["side"],
        "score": round(res["score"], 2),
        "range_low": res["range_low"], "range_high": res["range_high"],
        "sweep": res["sweep"], "entry": res["entry"], "target": res["target"],
        "flags": "+".join(res["flags"]),
        "status": "OPEN", "result": "", "r_multiple": "",
        "resolved_time": "",
    })
    save_log(rows)


def resolve_open_alerts(max_bars=100):
    """Check OPEN alerts: did price hit target or stop (sweep) first?"""
    rows = load_log()
    changed = False
    now = time.time()
    for r in rows:
        if r["status"] != "OPEN":
            continue
        tf = r["tf"]
        alert_ts = float(r["alert_ts"])
        bars_since = int((now - alert_ts) / (TF_MINUTES[tf] * 60))
        if bars_since < 1:
            continue
        try:
            candles = get_klines(r["symbol"], tf,
                                 limit=min(bars_since + 2, 500))
        except Exception:
            continue
        entry = float(r["entry"])
        target = float(r["target"])
        stop = float(r["sweep"])
        long = r["side"] == "LONG"
        risk = abs(entry - stop)
        result = None
        for c in candles:
            if c["t"] < alert_ts:
                continue
            hit_t = c["h"] >= target if long else c["l"] <= target
            hit_s = c["l"] <= stop if long else c["h"] >= stop
            if hit_t and hit_s:
                result = ("LOSS", -1.0)      # both in one candle: conservative
            elif hit_t:
                result = ("WIN", abs(target - entry) / risk if risk else 0)
            elif hit_s:
                result = ("LOSS", -1.0)
            if result:
                break
        if not result and bars_since > max_bars:
            result = ("EXPIRED", 0.0)
        if result:
            r["status"] = "CLOSED"
            r["result"] = result[0]
            r["r_multiple"] = round(result[1], 2)
            r["resolved_time"] = time.strftime("%Y-%m-%d %H:%M",
                                               time.gmtime(now))
            changed = True
            print("RESOLVED {} {} {} -> {} ({}R)".format(
                r["symbol"], r["tf"], r["side"], result[0], result[1]))
    if changed:
        save_log(rows)


def print_stats():
    rows = [r for r in load_log() if r["status"] == "CLOSED"
            and r["result"] in ("WIN", "LOSS")]
    if not rows:
        print("No closed alerts yet.")
        return
    def bucket(rows, keyfn):
        out = {}
        for r in rows:
            out.setdefault(keyfn(r), []).append(r)
        return out
    print("=== Po3 alert performance ===")
    for name, groups in [("BY TF", bucket(rows, lambda r: r["tf"])),
                         ("BY SIDE", bucket(rows, lambda r: r["side"])),
                         ("BY FLAGS", bucket(rows, lambda r: r["flags"] or "-"))]:
        print("\n--", name)
        for k in sorted(groups):
            g = groups[k]
            wins = [r for r in g if r["result"] == "WIN"]
            avg_r = sum(float(r["r_multiple"]) for r in g) / len(g)
            print("  {:12s} n={:3d}  winrate={:5.1f}%  avgR={:+.2f}".format(
                k, len(g), 100.0 * len(wins) / len(g), avg_r))


# ----------------------- Main loop -----------------------
def scan_once(args, state):
    resolve_open_alerts()
    symbols = get_symbols(args.min_vol)[:args.max_symbols]
    now = time.time()
    htf_cache = {}
    hits = 0
    for tf in args.tfs:
        cooldown = TF_MINUTES[tf] * 60 * args.cooldown_bars
        print("[{}] scanning {} symbols on {} ...".format(
            time.strftime("%H:%M:%S"), len(symbols), tf))
        for sym in symbols:
            try:
                candles = get_klines(sym, tf)
            except Exception as e:
                print("  ! {}: {}".format(sym, e))
                continue
            for side in ["long", "short"]:
                res = analyze(candles, side, args)
                if not res:
                    continue

                # --- kill zone filter (sweep candle time) ---
                kz = in_killzone(res["sweep_ts"])
                if args.killzone and not kz:
                    continue
                if kz:
                    res["score"] += 0.5
                    res["flags"].append("KZ-" + kz)

                # --- HTF bias filter ---
                if args.htf:
                    hkey = (sym, HTF_OF[tf])
                    if hkey not in htf_cache:
                        try:
                            htf_cache[hkey] = htf_bias(
                                get_klines(sym, HTF_OF[tf], limit=60))
                        except Exception:
                            htf_cache[hkey] = None
                    bias = htf_cache[hkey]
                    if bias is not None and bias != side:
                        continue
                    if bias == side:
                        res["flags"].append("HTF-" + HTF_OF[tf])

                # --- OI confirmation (only for surviving candidates) ---
                if args.oi:
                    try:
                        oi = get_oi_change_pct(sym, tf, args.manip_len)
                    except Exception:
                        oi = None
                    if oi is not None and oi <= -args.oi_drop:
                        res["score"] += 1.0
                        res["flags"].append("OI{:+.1f}%".format(oi))

                if res["score"] < args.min_score:
                    continue
                key = "{}|{}|{}".format(sym, tf, side)
                if now - state.get(key, 0) < cooldown:
                    continue
                state[key] = now
                hits += 1
                subject = "Po3 RETEST {} {} [{}] score={:.1f}".format(
                    sym, tf, res["side"], res["score"])
                body = (
                    "Symbol : {}\nTF     : {}\nSide   : {}\nScore  : {:.2f}\n"
                    "Range  : {} - {}\nSweep  : {} (stop)\nEntry  : {}\n"
                    "Target : {} (-0.272 ext)\nFlags  : {}".format(
                        sym, tf, res["side"], res["score"],
                        res["range_low"], res["range_high"],
                        res["sweep"], res["entry"], res["target"],
                        " ".join(res["flags"]) or "-"))
                print("ALERT ->", subject)
                alert(subject, body)
                log_alert(res, sym, tf, now)
            time.sleep(0.12)
    save_state(state)
    print("[{}] cycle done, {} alert(s).".format(
        time.strftime("%H:%M:%S"), hits))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", nargs="+", default=TFS, choices=TFS)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--min-vol", type=float, default=20000000.0)
    ap.add_argument("--max-symbols", type=int, default=120)
    ap.add_argument("--acc-len", type=int, default=40)
    ap.add_argument("--manip-len", type=int, default=20)
    ap.add_argument("--width-mult", type=float, default=20.0)
    ap.add_argument("--drift", type=float, default=0.70)
    ap.add_argument("--retest-tol", type=float, default=0.15)
    ap.add_argument("--cooldown-bars", type=int, default=8)
    ap.add_argument("--disp-mult", type=float, default=2.0,
                    help="displacement: reclaim body >= N x avg body")
    ap.add_argument("--oi-drop", type=float, default=1.0,
                    help="OI drop %% during sweep to count as confirmation")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="suppress alerts below this score")
    ap.add_argument("--no-htf", dest="htf", action="store_false",
                    help="disable HTF bias filter")
    ap.add_argument("--no-killzone", dest="killzone", action="store_false",
                    help="disable kill zone filter")
    ap.add_argument("--no-oi", dest="oi", action="store_false",
                    help="disable OI confirmation")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--stats", action="store_true",
                    help="print win rate / avg R report and exit")
    args = ap.parse_args()

    if args.test_alert:
        alert("Po3 monitor test", "Alert kanalin calisiyor.")
        return
    if args.stats:
        print_stats()
        return

    state = load_state()
    if args.once:
        scan_once(args, state)
        return
    print("Po3 monitor v4 running. TFs: {}  interval: {}s  "
          "htf={} killzone={} oi={}".format(
              args.tfs, args.interval, args.htf, args.killzone, args.oi))
    while True:
        try:
            scan_once(args, state)
        except Exception as e:
            print("Cycle error:", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
