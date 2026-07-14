#!/usr/bin/env python3
"""
Po3 / AMD scanner v2 "reclaim quality" — Binance USDT-M Futures.

loose.py'nin ustune 4 fake-reclaim filtresi ekler:
  1. Displacement : reclaim mumu gucclu govdeli olmali (>= disp-mult x ort govde)
  2. Reclaim hacmi: reclaim mumunun hacmi >= reclaim-vol x akumulasyon ort hacmi
  3. Kapanis derinligi: reclaim/son kapanis RL + (close-depth x range) ustunde olmali
  4. Ardisik kapanis: son consec mum RL ustunde kapanmis olmali
  + SSL kontrolu: sweep dibinin ALTINDA hala alinmamis dip (likidite) kaldiysa
    uyari basar (fiyat donup onu alabilir = fake reclaim riski)

Usage:
    python3 po3_loose_v2.py --tf 15m --top 5 --min-vol 5000000 --max-symbols 250
    python3 po3_loose_v2.py --tf 15m --strict          # 4 filtre de zorunlu
"""

import argparse
import time
import requests

BASE = "https://fapi.binance.com"


def get_symbols(min_quote_vol):
    tickers = requests.get(BASE + "/fapi/v1/ticker/24hr", timeout=10).json()
    syms = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) >= min_quote_vol
    ]
    syms.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in syms]


def get_klines(symbol, interval, limit=240):
    r = requests.get(
        BASE + "/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [
        {
            "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]),
            "v": float(k[7]),
        }
        for k in r.json()
    ]


def unswept_lows_below(candles, level, k=2):
    """level'in ALTINDA kalan, sonradan hic kirilmamis swing dipler (SSL)."""
    pools = []
    n = len(candles)
    for i in range(k, n - k):
        c = candles[i]
        if c["l"] >= level:
            continue
        if (all(c["l"] < candles[j]["l"] for j in range(i - k, i)) and
                all(c["l"] < candles[j]["l"] for j in range(i + 1, i + k + 1))):
            if all(candles[j]["l"] > c["l"] for j in range(i + k + 1, n)):
                pools.append(c["l"])
    return pools


def analyze(candles, args):
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

    # manipulasyon: RL altina sweep
    sweep_idx = min(range(len(recent)), key=lambda i: recent[i]["l"])
    sweep_low = recent[sweep_idx]["l"]
    if sweep_low >= rl:
        return None
    sweep_depth = (rl - sweep_low) / width

    # reclaim: range icine geri donus
    if not (rl < last["c"] < rh):
        return None
    pos = (last["c"] - rl) / width

    acc_vol = sum(c["v"] for c in acc) / len(acc)

    # --- RECLAIM KALITE METRIKLERI ---
    # sweep sonrasi RL ustunde kapanan mumlar icinden en guclu govdeli olan
    reclaims = [c for c in recent[sweep_idx:] if c["c"] > rl]
    if not reclaims:
        return None
    rec = max(reclaims, key=lambda c: abs(c["c"] - c["o"]))
    rec_body = abs(rec["c"] - rec["o"]) / avg_body if avg_body else 0.0
    rec_vol = rec["v"] / acc_vol if acc_vol else 0.0

    # kapanis derinligi: son kapanis RL'nin ne kadar ustunde
    close_depth_ok = last["c"] >= rl + args.close_depth * width

    # ardisik kapanis: sondan geriye kac mum RL ustunde kapanmis
    consec = 0
    for c in reversed(recent):
        if c["c"] > rl:
            consec += 1
        else:
            break

    # altta kalan alinmamis likidite (fake reclaim riski)
    ssl_below = unswept_lows_below(candles, sweep_low)

    checks = {
        "DISP": rec_body >= args.disp_mult,
        "RVOL": rec_vol >= args.reclaim_vol,
        "DEPTH": close_depth_ok,
        "CONSEC": consec >= args.consec,
    }
    if args.strict and not all(checks.values()):
        return None
    if args.strict and ssl_below:
        return None

    depth_score = 1.0 - abs(sweep_depth - 0.35) if sweep_depth < 1.0 else 0.0
    pos_score = 1.0 - abs(pos - 0.35)
    score = (2.0 * depth_score) + (1.5 * pos_score)
    score += 1.0 if checks["DISP"] else 0.0
    score += 1.0 if checks["RVOL"] else 0.0
    score += 0.5 if checks["DEPTH"] else 0.0
    score += 0.5 if checks["CONSEC"] else 0.0
    score -= 1.0 if ssl_below else 0.0     # altta likidite kaldiysa ceza

    return {
        "score": round(score, 3),
        "range_low": rl, "range_high": rh,
        "sweep_low": sweep_low,
        "sweep_depth_pct": round(sweep_depth * 100, 1),
        "last_close": last["c"],
        "pos_pct": round(pos * 100, 1),
        "reclaim_body": round(rec_body, 2),
        "reclaim_vol": round(rec_vol, 2),
        "consec": consec,
        "checks": "".join(("+" if v else "-") + k for k, v in checks.items()),
        "ssl_below": (min(ssl_below) if ssl_below else None),
        "target_ext_1272": round(rh + 0.272 * width, 6),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m", choices=["15m","30m","1h","2h","4h","1d"])
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--min-vol", type=float, default=5_000_000)
    ap.add_argument("--max-symbols", type=int, default=250)
    ap.add_argument("--acc-len", type=int, default=40)
    ap.add_argument("--manip-len", type=int, default=20)
    ap.add_argument("--width-mult", type=float, default=25.0)
    ap.add_argument("--drift", type=float, default=0.80)
    ap.add_argument("--disp-mult", type=float, default=2.0,
                    help="reclaim govdesi >= N x ort govde")
    ap.add_argument("--reclaim-vol", type=float, default=1.5,
                    help="reclaim hacmi >= N x aku ort hacim")
    ap.add_argument("--close-depth", type=float, default=0.15,
                    help="son kapanis RL + (N x range) ustunde olmali")
    ap.add_argument("--consec", type=int, default=2,
                    help="RL ustunde ardisik kapanis sayisi")
    ap.add_argument("--strict", action="store_true",
                    help="4 kalite filtresi + SSL kontrolu ZORUNLU")
    args = ap.parse_args()

    symbols = get_symbols(args.min_vol)[: args.max_symbols]
    print("Scanning {} symbols on {} (strict={}) ...".format(
        len(symbols), args.tf, args.strict))

    results = []
    for i, sym in enumerate(symbols, 1):
        try:
            res = analyze(get_klines(sym, args.tf), args)
            if res:
                res["symbol"] = sym
                results.append(res)
        except Exception as e:
            print("  ! {}: {}".format(sym, e))
        if i % 20 == 0:
            print("  ... {}/{}".format(i, len(symbols)))
        time.sleep(0.15)

    results.sort(key=lambda r: r["score"], reverse=True)
    print("\n=== Top {} Po3 candidates ({}) ===".format(args.top, args.tf))
    for r in results[: args.top]:
        ssl_line = ("\n  !! SSL below : {} (altta alinmamis dip var, "
                    "fake reclaim riski)".format(r["ssl_below"])
                    if r["ssl_below"] else "")
        print(
            "\n{}  score={}"
            "\n  Range       : {} - {}"
            "\n  Sweep low   : {} ({}% below range)"
            "\n  Last close  : {} ({}% into range)"
            "\n  Reclaim     : body {}x | vol {}x | consec {} | {}"
            "{}"
            "\n  -0.272 ext  : {}".format(
                r["symbol"], r["score"],
                r["range_low"], r["range_high"],
                r["sweep_low"], r["sweep_depth_pct"],
                r["last_close"], r["pos_pct"],
                r["reclaim_body"], r["reclaim_vol"], r["consec"], r["checks"],
                ssl_line,
                r["target_ext_1272"],
            )
        )
    if not results:
        print("No candidates. --strict kapatmayi veya esikleri dusurmeyi dene.")


if __name__ == "__main__":
    main()
