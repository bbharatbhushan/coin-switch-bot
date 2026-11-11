"""
CoinSwitch BTC 1-minute scalping bot  (PAPER MODE by default)

Usage:
    pip install requests cryptography
    export COINSWITCH_API_KEY=...  COINSWITCH_SECRET_KEY=...   # only needed for live
    python coinswitch_scalping_bot.py run      # paper trading (PAPER=True)
    python coinswitch_scalping_bot.py report   # performance report from trades.csv

IMPORTANT
- Endpoints/payloads below are PLACEHOLDERS: verify against the current CoinSwitch PRO API docs.
  As far as I know CoinSwitch exposes spot (and possibly futures) APIs, NOT BTC options.
  So this bot trades BTC/INR spot with an abstraction you can swap for another instrument.
- Test in PAPER mode for weeks before risking real money. No strategy guarantees profit.
"""
import os, sys, csv, time, logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, date
from urllib.parse import urlencode, unquote_plus

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ------------------------------------------------------------------ CONFIG
@dataclass
class Config:
    PAPER: bool = True
    SYMBOL: str = "BTC/INR"
    EXCHANGE: str = "coinswitchx"
    START_EQUITY: float = 100000.0     # INR (paper) – live mode: fetch real balance
    POLL_SECONDS: float = 1.0          # keep within API rate limits
    # indicators
    EMA_FAST: int = 9
    EMA_SLOW: int = 21
    RSI_LEN: int = 14
    ATR_LEN: int = 14
    MIN_SCORE: int = 3                 # confluence needed to enter
    ALLOW_SHORT: bool = False          # spot cannot short; enable only on futures
    # risk management
    RISK_PER_TRADE: float = 0.005      # 0.5% of equity risked per trade
    MAX_POSITION_PCT: float = 0.25     # never put >25% equity in one position
    SL_ATR_MULT: float = 1.2
    TP_R_MULT: float = 2.0
    BREAKEVEN_R: float = 1.0           # move SL to breakeven after +1R
    TRAIL_ATR_MULT: float = 1.0        # dynamic trailing distance
    MAX_HOLD_CANDLES: int = 15         # time stop
    DAILY_LOSS_LIMIT: float = 0.03     # stop trading for the day at -3%
    MAX_CONSEC_LOSSES: int = 3
    COOLDOWN_CANDLES: int = 5
    FEE_PCT: float = 0.001             # 0.1% per side (check your tier!)
    SLIPPAGE_PCT: float = 0.0003
    TRADE_LOG: str = "trades.csv"

# ------------------------------------------------------------------ API CLIENT
class CoinSwitchClient:
    BASE = "https://coinswitch.co"
    TICKER = "/trade/api/v2/24hr/ticker"   # VERIFY
    ORDER = "/trade/api/v2/order"          # VERIFY

    def __init__(self, api_key, secret_hex):
        self.key = api_key
        self.sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_hex))
        self.s = requests.Session()        # keep-alive connection = lower latency
        self.s.headers.update({"Content-Type": "application/json"})

    def _headers(self, method, path):
        epoch = str(int(time.time() * 1000))
        sig = self.sk.sign((method + path + epoch).encode()).hex()
        return {"X-AUTH-SIGNATURE": sig, "X-AUTH-APIKEY": self.key, "X-AUTH-EPOCH": epoch}

    def get_price(self, cfg):
        params = {"exchange": cfg.EXCHANGE, "symbol": cfg.SYMBOL}
        path = unquote_plus(self.TICKER + "?" + urlencode(params))
        r = self.s.get(self.BASE + path, headers=self._headers("GET", path), timeout=3)
        r.raise_for_status()
        d = r.json()["data"][cfg.EXCHANGE][cfg.SYMBOL]   # VERIFY response shape
        return float(d["lastPrice"])

    def market_order(self, cfg, side, qty):
        body = {"side": side.lower(), "symbol": cfg.SYMBOL, "type": "market",
                "quantity": qty, "exchange": cfg.EXCHANGE}
        r = self.s.post(self.BASE + self.ORDER, json=body,
                        headers=self._headers("POST", self.ORDER), timeout=3)
        r.raise_for_status()
        return r.json()

# ------------------------------------------------------------------ BROKERS
class PaperBroker:
    def __init__(self, cfg): self.cfg = cfg
    def execute(self, side, qty, price):
        slip = price * self.cfg.SLIPPAGE_PCT
        return price + slip if side == "BUY" else price - slip

class LiveBroker:
    def __init__(self, cfg, client): self.cfg, self.client = cfg, client
    def execute(self, side, qty, price):
        t0 = time.perf_counter()
        self.client.market_order(self.cfg, side, qty)
        logging.info("order latency %.0f ms", (time.perf_counter() - t0) * 1000)
        return price  # replace with actual fill price from order response

# ------------------------------------------------------------------ CANDLES
class CandleBuilder:
    def __init__(self, maxlen=300):
        self.candles = deque(maxlen=maxlen)   # dict: t,o,h,l,c
        self.cur = None
    def update(self, price, ts):
        minute = int(ts // 60)
        if self.cur is None or self.cur["t"] != minute:
            closed = self.cur
            if closed: self.candles.append(closed)
            self.cur = {"t": minute, "o": price, "h": price, "l": price, "c": price}
            return closed
        c = self.cur
        c["h"], c["l"], c["c"] = max(c["h"], price), min(c["l"], price), price
        return None

# ------------------------------------------------------------------ INDICATORS
def ema(vals, n):
    k, e = 2 / (n + 1), vals[0]
    for v in vals[1:]: e = v * k + e * (1 - k)
    return e

def rsi(closes, n=14):
    g = l = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        g, l = g + max(d, 0), l + max(-d, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

def atr(cs, n=14):
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
           for p, c in zip(cs[-n - 1:-1], cs[-n:])]
    return sum(trs) / len(trs)

# ------------------------------------------------------------------ PATTERNS
def candle_pattern(cs):
    p, c = cs[-2], cs[-1]
    body, rng = abs(c["c"] - c["o"]), (c["h"] - c["l"]) or 1e-9
    lower, upper = min(c["o"], c["c"]) - c["l"], c["h"] - max(c["o"], c["c"])
    if p["c"] < p["o"] and c["c"] > c["o"] and c["c"] >= p["o"] and c["o"] <= p["c"]: return 1, "bull_engulfing"
    if p["c"] > p["o"] and c["c"] < c["o"] and c["c"] <= p["o"] and c["o"] >= p["c"]: return -1, "bear_engulfing"
    if lower > 2 * body and upper < body and body / rng > 0.05: return 1, "hammer"
    if upper > 2 * body and lower < body and body / rng > 0.05: return -1, "shooting_star"
    return 0, ""

def price_action(cs, lookback=5):
    prev = cs[-lookback - 1:-1]
    if cs[-1]["c"] > max(x["h"] for x in prev): return 1, "breakout_high"
    if cs[-1]["c"] < min(x["l"] for x in prev): return -1, "breakdown_low"
    return 0, ""

def double_pattern(cs, n=30, tol=0.001):
    w = cs[-n:]
    lows = sorted(range(len(w)), key=lambda i: w[i]["l"])[:2]
    highs = sorted(range(len(w)), key=lambda i: -w[i]["h"])[:2]
    i, j = sorted(lows)
    if j - i >= 5 and abs(w[i]["l"] - w[j]["l"]) / w[i]["l"] < tol and w[-1]["c"] > max(x["h"] for x in w[i:j]):
        return 1, "double_bottom"
    i, j = sorted(highs)
    if j - i >= 5 and abs(w[i]["h"] - w[j]["h"]) / w[i]["h"] < tol and w[-1]["c"] < min(x["l"] for x in w[i:j]):
        return -1, "double_top"
    return 0, ""

def fib_signal(cs, n=30, tol=0.0008):
    w = cs[-n:]
    hi_i = max(range(len(w)), key=lambda i: w[i]["h"]); lo_i = min(range(len(w)), key=lambda i: w[i]["l"])
    hi, lo, px = w[hi_i]["h"], w[lo_i]["l"], w[-1]["c"]
    if hi - lo <= 0: return 0, ""
    for r in (0.5, 0.618):
        if lo_i < hi_i and abs(px - (hi - r * (hi - lo))) / px < tol: return 1, f"fib_{r}_pullback_up"
        if hi_i < lo_i and abs(px - (lo + r * (hi - lo))) / px < tol: return -1, f"fib_{r}_pullback_down"
    return 0, ""

# ------------------------------------------------------------------ STRATEGY
def evaluate(cs, cfg):
    """Return (direction, score, reasons, atr). Needs >= 40 candles."""
    closes = [c["c"] for c in cs]
    f, s = ema(closes[-60:], cfg.EMA_FAST), ema(closes[-60:], cfg.EMA_SLOW)
    r, a = rsi(closes, cfg.RSI_LEN), atr(cs, cfg.ATR_LEN)
    votes = []
    votes.append((1 if f > s else -1, f"ema{cfg.EMA_FAST}{'>' if f > s else '<'}ema{cfg.EMA_SLOW}"))
    if 45 < r < 70: votes.append((1, f"rsi{r:.0f}"))
    elif 30 < r < 55: votes.append((-1, f"rsi{r:.0f}"))
    for fn in (candle_pattern, price_action, double_pattern, fib_signal):
        d, name = fn(cs)
        if d: votes.append((d, name))
    long_s = sum(1 for d, _ in votes if d == 1); short_s = sum(1 for d, _ in votes if d == -1)
    if long_s >= cfg.MIN_SCORE and long_s > short_s:
        return 1, long_s, [n for d, n in votes if d == 1], a
    if cfg.ALLOW_SHORT and short_s >= cfg.MIN_SCORE and short_s > long_s:
        return -1, short_s, [n for d, n in votes if d == -1], a
    return 0, 0, [], a

# ------------------------------------------------------------------ POSITION / RISK
@dataclass
class Position:
    side: int; entry: float; qty: float; sl: float; sl0: float; tp: float
    risk: float; atr: float; indicators: str; opened: str
    best: float = 0.0; candles: int = 0
    def __post_init__(self): self.best = self.entry

class RiskManager:
    def __init__(self, cfg):
        self.cfg, self.equity = cfg, cfg.START_EQUITY
        self.day, self.day_start = date.today(), cfg.START_EQUITY
        self.consec, self.cooldown = 0, 0
    def can_trade(self):
        if date.today() != self.day:
            self.day, self.day_start, self.consec = date.today(), self.equity, 0
        if self.equity <= self.day_start * (1 - self.cfg.DAILY_LOSS_LIMIT): return False
        if self.consec >= self.cfg.MAX_CONSEC_LOSSES: return False
        return self.cooldown <= 0
    def size(self, entry, sl):
        risk_amt = self.equity * self.cfg.RISK_PER_TRADE
        qty = risk_amt / abs(entry - sl)
        return min(qty, self.equity * self.cfg.MAX_POSITION_PCT / entry)
    def record(self, pnl):
        self.equity += pnl
        self.consec = self.consec + 1 if pnl < 0 else 0
        self.cooldown = self.cfg.COOLDOWN_CANDLES

# ------------------------------------------------------------------ TRADE LOG
FIELDS = ["id", "opened", "closed", "side", "entry", "exit", "qty", "sl_initial", "sl_final",
          "tp", "indicators", "exit_reason", "pnl", "equity"]

class TradeLog:
    def __init__(self, path):
        self.path, self.n = path, 0
        if not os.path.exists(path):
            with open(path, "w", newline="") as f: csv.writer(f).writerow(FIELDS)
        else: self.n = sum(1 for _ in open(path)) - 1
    def add(self, pos, exit_px, reason, pnl, equity):
        self.n += 1
        row = [self.n, pos.opened, datetime.now().isoformat(timespec="seconds"),
               "LONG" if pos.side == 1 else "SHORT", round(pos.entry, 2), round(exit_px, 2),
               round(pos.qty, 6), round(pos.sl0, 2), round(pos.sl, 2), round(pos.tp, 2),
               pos.indicators, reason, round(pnl, 2), round(equity, 2)]
        with open(self.path, "a", newline="") as f: csv.writer(f).writerow(row)

def report(path="trades.csv", start_equity=100000.0):
    rows = list(csv.DictReader(open(path)))
    if not rows: return print("No trades yet.")
    pnls = [float(r["pnl"]) for r in rows]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
    eq, peak, mdd = start_equity, start_equity, 0.0
    for p in pnls:
        eq += p; peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    gl = abs(sum(losses))
    out = [
        f"Trades            : {len(pnls)}",
        f"Win rate          : {len(wins) / len(pnls):.1%}",
        f"Avg profit/trade  : {sum(wins) / len(wins) if wins else 0:.2f}",
        f"Avg loss/trade    : {sum(losses) / len(losses) if losses else 0:.2f}",
        f"Net P&L           : {sum(pnls):.2f}",
        f"Profit factor     : {sum(wins) / gl if gl else float('inf'):.2f}",
        f"Expectancy/trade  : {sum(pnls) / len(pnls):.2f}",
        f"Max drawdown      : {mdd:.2%}",
    ]
    print("\n".join(out)); open("report.txt", "w").write("\n".join(out))

# ------------------------------------------------------------------ BOT LOOP
def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = Config()
    client = None
    if not cfg.PAPER:
        client = CoinSwitchClient(os.environ["COINSWITCH_API_KEY"], os.environ["COINSWITCH_SECRET_KEY"])
    broker = PaperBroker(cfg) if cfg.PAPER else LiveBroker(cfg, client)
    price_src = client or CoinSwitchClient(os.environ.get("COINSWITCH_API_KEY", ""), os.environ.get("COINSWITCH_SECRET_KEY", "00" * 32))
    cb, risk, log, pos = CandleBuilder(), RiskManager(cfg), TradeLog(cfg.TRADE_LOG), None

    def close(px, reason):
        nonlocal pos
        side = "SELL" if pos.side == 1 else "BUY"
        fill = broker.execute(side, pos.qty, px)
        gross = (fill - pos.entry) * pos.qty * pos.side
        fees = (pos.entry + fill) * pos.qty * cfg.FEE_PCT
        pnl = gross - fees
        risk.record(pnl); log.add(pos, fill, reason, pnl, risk.equity)
        logging.info("CLOSE %s @%.2f pnl=%.2f equity=%.2f", reason, fill, pnl, risk.equity)
        pos = None

    logging.info("Bot started | PAPER=%s", cfg.PAPER)
    try:
        while True:
            t0 = time.perf_counter()
            try: px = price_src.get_price(cfg)
            except Exception as e:
                logging.warning("price error: %s", e); time.sleep(2); continue
            closed = cb.update(px, time.time())

            # --- tick-level management of open position (SL / TP / dynamic trail)
            if pos:
                pos.best = max(pos.best, px) if pos.side == 1 else min(pos.best, px)
                r_now = (px - pos.entry) * pos.side / pos.risk
                if r_now >= cfg.BREAKEVEN_R:  # breakeven + ATR trail
                    trail = pos.best - pos.side * cfg.TRAIL_ATR_MULT * pos.atr
                    be = pos.entry + pos.side * pos.entry * 2 * cfg.FEE_PCT
                    new_sl = max(trail, be) if pos.side == 1 else min(trail, be)
                    pos.sl = max(pos.sl, new_sl) if pos.side == 1 else min(pos.sl, new_sl)
                if (px - pos.sl) * pos.side <= 0: close(px, "stop/trail")
                elif (px - pos.tp) * pos.side >= 0: close(px, "take_profit")

            # --- candle-close logic: time stop + new entries
            if closed:
                risk.cooldown -= 1
                if pos:
                    pos.candles += 1
                    if pos.candles >= cfg.MAX_HOLD_CANDLES: close(px, "time_stop")
                if not pos and len(cb.candles) >= 40 and risk.can_trade():
                    d, score, why, a = evaluate(list(cb.candles), cfg)
                    if d:
                        sl_est = px - d * cfg.SL_ATR_MULT * a
                        qty = risk.size(px, sl_est)
                        if qty > 0:
                            entry = broker.execute("BUY" if d == 1 else "SELL", qty, px)
                            sl = entry - d * cfg.SL_ATR_MULT * a
                            rd = abs(entry - sl)
                            pos = Position(d, entry, qty, sl, sl, entry + d * cfg.TP_R_MULT * rd, rd, a,
                                           f"score={score}:" + "+".join(why), datetime.now().isoformat(timespec="seconds"))
                            logging.info("OPEN %s @%.2f sl=%.2f tp=%.2f qty=%.6f [%s]",
                                         "LONG" if d == 1 else "SHORT", entry, sl, pos.tp, qty, pos.indicators)
            time.sleep(max(0, cfg.POLL_SECONDS - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        if pos: close(cb.cur["c"], "manual_stop")
        logging.info("Stopped.")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    report() if cmd == "report" else run()