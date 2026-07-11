#!/usr/bin/env python3
"""
Po3 / AMD monitor v3 — Binance USDT-M Futures, stdlib only (Python 3.6+).

Watches 15m / 30m / 1h / 4h continuously and sends a Telegram and/or e-mail
alert when a symbol completes the sequence:
    accumulation range -> manipulation sweep -> reclaim back into range
    -> RETEST of the swept boundary (entry zone)

Usage:
    # 1) Fill in the CONFIG block below (Telegram token/chat id and/or SMTP)
    # 2) Test your alert channel:
    python3 po3_monitor_v3.py --test-alert
    # 3) Run one scan and exit:
    python3 po3_monitor_v3.py --once
    # 4) Run forever (background):
    nohup python3 po3_monitor_v3.py > po3.log 2>&1 &
"""

import argparse
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
    "SMTP_USER": "",                 # sender address / login
    "SMTP_PASS": "",                 # app password (Gmail: App Password)
    "MAIL_TO": "",                   # recipient
}
# ==============================================================

BASE = "https://fapi.binance.com"
CTX = ssl.create_default_context()
TFS = ["15m", "30m", "1h", "4h"]
TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "po3_alert_state.json")


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
            "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]),
            "v": float(k[7]),
        }
        for k in http_get(url)
    ]


# ----------------------- Alert channels -----------------------
def send_telegram(text):
    tok = CONFIG["TELEGRAM_BOT_TOKEN"]
    if not tok:
        return False
    data = urllib.parse.urlencode({
        "chat_id": CONFIG["TELEGRAM_CHAT_ID"],
        "text": text,
    }).encode()
    url = "https://api.telegram.org/bot{}/sendMessage".format(tok)
    try:
        req = urllib.request.Request(url, data=data)
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
    ok = False
    ok = send_telegram(subject + "\n" + body) or ok
    ok = send_email(subject, body) or ok
    if not ok:
        print("!! No alert channel configured/working. Message was:\n", body)


# ----------------------- Po3 analysis -----------------------
def analyze(candles, side, acc_len, manip_len, width_mult, drift_max,
            retest_tol):
    """Returns setup dict when the RETEST condition is met, else None."""
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
    if width > width_mult * avg_body:
        return None
    if abs(acc[-1]["c"] - acc[0]["o"]) > drift_max * width:
        return None

    if side == "long":
        sweep = min(c["l"] for c in recent)
        if sweep >= rl:
            return None
        # reclaim: at least one recent candle CLOSED back inside the range
        if not any(c["c"] > rl for c in recent):
            return None
        # RETEST: last candle dips into the zone just above range low
        # (low touches rl +/- retest_tol*width) and closes back inside
        if not (last["l"] <= rl + retest_tol * width and last["c"] > rl):
            return None
        if last["c"] >= rh:            # already expanded -> too late
            return None
        target = rh + 0.272 * width
        zone = "retest of range low {}".format(rl)
    else:
        sweep = max(c["h"] for c in recent)
        if sweep <= rh:
            return None
        if not any(c["c"] < rh for c in recent):
            return None
        if not (last["h"] >= rh - retest_tol * width and last["c"] < rh):
            return None
        if last["c"] <= rl:
            return None
        target = rl - 0.272 * width
        zone = "retest of range high {}".format(rh)

    return {
        "side": side.upper(),
        "range_low": rl,
        "range_high": rh,
        "sweep": sweep,
        "last_close": last["c"],
        "target": round(target, 6),
        "zone": zone,
    }


# ----------------------- State / dedup -----------------------
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


# ----------------------- Main loop -----------------------
def scan_once(args, state):
    symbols = get_symbols(args.min_vol)[:args.max_symbols]
    now = time.time()
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
                res = analyze(candles, side, args.acc_len, args.manip_len,
                              args.width_mult, args.drift, args.retest_tol)
                if not res:
                    continue
                key = "{}|{}|{}".format(sym, tf, side)
                if now - state.get(key, 0) < cooldown:
                    continue          # already alerted recently
                state[key] = now
                hits += 1
                subject = "Po3 RETEST {} {} [{}]".format(sym, tf, res["side"])
                body = (
                    "Symbol : {}\nTF     : {}\nSide   : {}\n"
                    "Range  : {} - {}\nSweep  : {}\nClose  : {}\n"
                    "Setup  : {}\nTarget (-0.272): {}".format(
                        sym, tf, res["side"],
                        res["range_low"], res["range_high"],
                        res["sweep"], res["last_close"],
                        res["zone"], res["target"]))
                print("ALERT ->", subject)
                alert(subject, body)
            time.sleep(0.12)
    save_state(state)
    print("[{}] scan done, {} alert(s).".format(time.strftime("%H:%M:%S"), hits))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", nargs="+", default=TFS,
                    choices=TFS, help="timeframes to watch")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between scan cycles (default 300)")
    ap.add_argument("--min-vol", type=float, default=20000000.0)
    ap.add_argument("--max-symbols", type=int, default=120)
    ap.add_argument("--acc-len", type=int, default=40)
    ap.add_argument("--manip-len", type=int, default=20)
    ap.add_argument("--width-mult", type=float, default=20.0)
    ap.add_argument("--drift", type=float, default=0.70)
    ap.add_argument("--retest-tol", type=float, default=0.15,
                    help="retest zone size as fraction of range width")
    ap.add_argument("--cooldown-bars", type=int, default=8,
                    help="don't re-alert same symbol/TF/side for N bars")
    ap.add_argument("--once", action="store_true", help="single scan, then exit")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test message to configured channels and exit")
    args = ap.parse_args()

    if args.test_alert:
        alert("Po3 monitor test", "Alert kanalin calisiyor.")
        return

    state = load_state()
    if args.once:
        scan_once(args, state)
        return

    print("Po3 monitor running. TFs: {}  interval: {}s".format(
        args.tfs, args.interval))
    while True:
        try:
            scan_once(args, state)
        except Exception as e:
            print("Cycle error:", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
