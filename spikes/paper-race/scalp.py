#!/usr/bin/env python3
"""s5: MU step scalp, both directions, on Alpaca PAPER. Throwaway spike, same rules as race.py (paper endpoint only).

Rule (in the Oct 13 – Nov 25 2026 one-minute study the short side had a thin edge and the long side lost;
both run here at the user's direction, to trade the oscillations either way):
  - Track swings of about $2.50 (0.451% of the day's opening price) in real time.
  - A step down = a lower swing high followed by a break of the previous swing low.
  - On the second or later consecutive step down, short QTY shares at the break with a tight limit,
    then attach a take-profit about $5 below and a stop about $5 above the fill (0.902% of the open).
  - Mirror for steps up (higher swing low, then a break of the previous swing high): go long.
  - A signal against an open position is ignored; exits are the target, the stop, or the close.

s6 is the options twin: on every s5 entry it also buys TWIN_CONTRACTS at-the-money calls (long) or puts
(short), nearest expiry that is not today, and sells them when the share trade ends. Same entries and exits,
so shares and options can be compared directly.
  - One position at a time, no new entries in the last 30 minutes, flat 6 minutes before the close.

Usage: scalp.py start | stop | test | twintest | run
"""
import os, signal, subprocess, sys, time, traceback
from datetime import datetime, timedelta

import race
from race import ApiError, DATA, ET, STATE, TRADE, api, log, positions, sessions, snapshots

SYMBOL, QTY = "MU", 200
SWING, TARGET, STOP = 0.00451, 0.00902, 0.00902  # fractions of the day's opening price
MAX_ENTRIES = 20   # per day, both directions combined
POLL = 4           # seconds between price checks
COLLAR = 0.10      # entry limit sits this far through the last price; we never chase further
LATE = 1.00        # skip the signal if price is already this far past the broken level
TWIN_CONTRACTS = 4  # s6: 4 at-the-money contracts move about like 200 shares
PID = STATE / "scalp.pid"


class Steps:
    """Real-time swing tracker. feed() returns (side, broken level) when a tick completes the second or
    later consecutive step: side -1 for a step down (short), +1 for a step up (long). Otherwise None."""

    def __init__(self, z, anchor):
        self.z, self.anchor, self.dir, self.ext = z, anchor, 0, None
        self.highs, self.lows, self.broke_low, self.broke_high, self.down, self.up = [], [], -1, -1, 0, 0

    def feed(self, h, l):
        z = self.z
        if self.dir == 0:
            if h >= self.anchor + z:
                self.dir, self.ext = 1, h
                self.lows.append(self.anchor)
            elif l <= self.anchor - z:
                self.dir, self.ext = -1, l
                self.highs.append(self.anchor)
        elif self.dir == 1:
            self.ext = max(self.ext, h)
            if l <= self.ext - z:
                self.highs.append(self.ext)
                self.dir, self.ext = -1, l
        else:
            self.ext = min(self.ext, l)
            if h >= self.ext + z:
                self.lows.append(self.ext)
                self.dir, self.ext = 1, h
        found = None
        if self.dir == -1 and self.lows and self.broke_low < len(self.lows) - 1 and l < self.lows[-1]:
            self.broke_low = len(self.lows) - 1
            self.down = self.down + 1 if len(self.highs) >= 2 and self.highs[-1] < self.highs[-2] else 1
            self.up = 0
            if self.down >= 2:
                found = (-1, self.lows[-1])
        if self.dir == 1 and self.highs and self.broke_high < len(self.highs) - 1 and h > self.highs[-1]:
            self.broke_high = len(self.highs) - 1
            self.up = self.up + 1 if len(self.lows) >= 2 and self.lows[-1] > self.lows[-2] else 1
            self.down = 0
            if self.up >= 2:
                found = (1, self.highs[-1])
        return found


def cid(tag, suffix=""):
    return f"race-s5-{race.today():%Y%m%d}-{SYMBOL}-{tag}{suffix}"


def flatten(tag, suffix=""):
    for o in api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=SYMBOL, nested="false")):
        try:
            api("DELETE", f"{TRADE}/orders/{o['id']}")
        except ApiError as e:
            log(f"  s5 cancel failed: {e}")
    for _ in range(10):  # wait until the cancelled exit pair has released the shares
        time.sleep(1)
        if not api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=SYMBOL)):
            break
    pos = positions().get(SYMBOL)
    if not pos:
        return
    qty = float(pos["qty"])
    o = api("POST", f"{TRADE}/orders", body=dict(symbol=SYMBOL, qty=str(int(abs(qty))), side="sell" if qty > 0 else "buy", type="market",
                                                 time_in_force="day", client_order_id=cid(tag, suffix)))
    log(f"  s5 FLATTEN {int(abs(qty))} {SYMBOL} → {o['status']}")


def enter(n, side, price, tgt, stp, qty=QTY, suffix=""):
    """Enter long (side 1) or short (side -1) with a tight limit; once filled, attach a one-cancels-other exit.
    Returns True if a position was opened."""
    word, exit_word = ("buy", "sell") if side == 1 else ("sell", "buy")
    limit = round(price + side * COLLAR, 2)
    try:
        o = api("POST", f"{TRADE}/orders", body=dict(symbol=SYMBOL, qty=str(qty), side=word, type="limit", limit_price=f"{limit:.2f}",
                                                     time_in_force="day", client_order_id=cid(f"e{n}", suffix)))
    except ApiError as e:
        log(f"  s5 ENTRY FAILED: {e}")
        return False
    for _ in range(20):
        time.sleep(1)
        o = api("GET", f"{TRADE}/orders/{o['id']}")
        if o["status"] == "filled":
            break
    else:
        try:
            api("DELETE", f"{TRADE}/orders/{o['id']}")
        except ApiError:
            pass
        time.sleep(1.5)
        o = api("GET", f"{TRADE}/orders/{o['id']}")
    filled = int(float(o["filled_qty"] or 0))
    if not filled:
        log(f"  s5 entry {n}: limit {limit} not filled in 20s, cancelled")
        return False
    fill = float(o["filled_avg_price"])
    take, stop = round(fill + side * tgt, 2), round(fill - side * stp, 2)
    try:
        api("POST", f"{TRADE}/orders", body=dict(symbol=SYMBOL, qty=str(filled), side=exit_word, type="limit", time_in_force="day", order_class="oco",
                                                 take_profit={"limit_price": f"{take:.2f}"}, stop_loss={"stop_price": f"{stop:.2f}"},
                                                 client_order_id=cid(f"x{n}", suffix)))
    except ApiError as e:
        log(f"  s5 EXIT ORDER FAILED ({e}); covering immediately")
        flatten(f"bail{n}", suffix)
        return False
    log(f"  s5 ENTRY {n}: {'long' if side == 1 else 'short'} {filled} {SYMBOL} @ {fill:.2f} | target {take:.2f} | stop {stop:.2f}")
    return True


def open_twin(n, side, spot, next_session, contracts=TWIN_CONTRACTS, suffix=""):
    """s6: buy at-the-money calls (long) or puts (short) alongside the share trade, so the same entries and
    exits can be compared in shares and in options. Nearest expiry that is not today. Returns the option symbol."""
    typ = "call" if side == 1 else "put"
    try:
        found = api("GET", f"{TRADE}/options/contracts", params=dict(
            underlying_symbols=SYMBOL, status="active", type=typ, limit=1000, strike_price_gte=round(spot * 0.97, 2), strike_price_lte=round(spot * 1.03, 2),
            expiration_date_gte=next_session.isoformat(), expiration_date_lte=(next_session + timedelta(days=10)).isoformat())).get("option_contracts", [])
        exp = min(c["expiration_date"] for c in found)
        c = min((c for c in found if c["expiration_date"] == exp), key=lambda c: abs(float(c["strike_price"]) - spot))
        o = api("POST", f"{TRADE}/orders", body=dict(symbol=c["symbol"], qty=str(contracts), side="buy", type="market", time_in_force="day",
                                                     client_order_id=f"race-s6-{race.today():%Y%m%d}-{c['symbol']}-e{n}{suffix}"))
        log(f"  s6 TWIN {n}: buy {contracts} {typ}s {c['symbol']} (strike {float(c['strike_price']):g}, exp {exp}) → {o['status']}")
        return c["symbol"]
    except Exception as e:
        log(f"  s6 twin entry failed: {e}")
        return None


def close_twin(symbol, n, suffix=""):
    try:
        for o in api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=symbol)):
            api("DELETE", f"{TRADE}/orders/{o['id']}")
            time.sleep(1)
        pos = positions().get(symbol)
        if not pos:
            return log(f"  s6 twin {symbol}: nothing to sell")
        qty = int(abs(float(pos["qty"])))
        o = api("POST", f"{TRADE}/orders", body=dict(symbol=symbol, qty=str(qty), side="sell", type="market", time_in_force="day",
                                                     client_order_id=f"race-s6-{race.today():%Y%m%d}-{symbol}-x{n}{suffix}"))
        log(f"  s6 TWIN {n}: sell {qty} {symbol} (bought near {float(pos['avg_entry_price']):.2f}, now {float(pos['current_price']):.2f}) → {o['status']}")
    except Exception as e:
        log(f"  s6 twin exit failed for {symbol}: {e}")


def trade_day(op, cl, next_session):
    while True:  # wait for today's opening price on the free feed
        snap = snapshots([SYMBOL]).get(SYMBOL, {})
        if snap.get("open") and snap.get("open_is_today"):
            break
        time.sleep(10)
    ref = snap["open"]
    z, tgt, stp = ref * SWING, ref * TARGET, ref * STOP
    steps = Steps(z, ref)
    bars = api("GET", f"{DATA}/v2/stocks/bars", params=dict(symbols=SYMBOL, timeframe="1Min", start=op.isoformat(), feed="iex", limit=10000))
    warm = (bars.get("bars") or {}).get(SYMBOL, [])
    for b in warm:  # rebuild today's swing structure so far; signals during warm-up are ignored
        steps.feed(b["h"], b["l"])
    today = f"race-s5-{race.today():%Y%m%d}-{SYMBOL}-e"
    done = [o for o in api("GET", f"{TRADE}/orders", params=dict(status="all", after=op.isoformat(), limit=500))
            if o["client_order_id"].startswith(today) and "-test" not in o["client_order_id"]]
    held = positions()
    entries, in_pos = len(done), SYMBOL in held
    # s4's overnight MU options are sold by race.py at open + 5 min; after that any MU option here is our twin
    twins_ok_from = op + timedelta(minutes=6)
    twin = next((s for s, p in held.items() if p.get("asset_class") == "us_option" and race.option_root(s) == SYMBOL), None) \
        if datetime.now(ET) >= twins_ok_from else None
    log(f"s5 scalp live: open {ref:.2f}, swing ${z:.2f}, target ${tgt:.2f}, stop ${stp:.2f}, {QTY} shares | warmed up on {len(warm)} bars, "
        f"{steps.down} step(s) down / {steps.up} up right now, {entries} entries already today, both directions | "
        f"s6 options twin: {TWIN_CONTRACTS} contracts per trade{', holding ' + twin if twin else ''}")
    while True:
        now = datetime.now(ET)
        if now >= cl - timedelta(minutes=6):
            log("s5 end of day: cancelling orders and flattening")
            flatten("flat")
            if twin:
                close_twin(twin, entries)
            return
        try:
            p = snapshots([SYMBOL])[SYMBOL]["price"]
            found = steps.feed(p, p)
            if in_pos:
                if SYMBOL not in positions():
                    in_pos = False
                    log("  s5 position closed (target or stop hit)")
            if not in_pos and twin:  # the twin always exits with the share trade
                close_twin(twin, entries)
                twin = None
            elif not in_pos and found and entries < MAX_ENTRIES and now < cl - timedelta(minutes=30):
                side, level = found
                if side * (p - level) > LATE:
                    log(f"  s5 signal at {level:.2f} skipped: price already {p:.2f}")
                else:
                    entries += 1
                    log(f"  s5 SIGNAL: step {'up' if side == 1 else 'down'} #{steps.up if side == 1 else steps.down}, broke {level:.2f}, price {p:.2f}")
                    in_pos = enter(entries, side, p, tgt, stp)
                    if in_pos and now >= twins_ok_from:
                        twin = open_twin(entries, side, p, next_session)
        except Exception:
            log(f"s5 loop error:\n{traceback.format_exc()}")
        time.sleep(POLL)


def run():
    log(f"s5 scalp runner started (pid {os.getpid()})")
    traded = set()
    while True:
        now = datetime.now(ET)
        try:
            sess = next((x for x in sessions() if x[0] == now.date()), None)
        except Exception as e:
            log(f"s5 calendar lookup failed: {e}")
            time.sleep(60)
            continue
        if sess and sess[0] not in traded and sess[1] + timedelta(seconds=90) <= now < sess[2] - timedelta(minutes=6):
            traded.add(sess[0])
            try:
                trade_day(sess[1], sess[2], next(x[0] for x in sessions() if x[0] > sess[0]))
            except Exception:
                log(f"s5 day failed:\n{traceback.format_exc()}")
                traded.discard(sess[0])
                time.sleep(30)
        else:
            time.sleep(20)


def alive():
    try:
        pid = int(PID.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start():
    STATE.mkdir(exist_ok=True)
    if alive():
        return print(f"already running (pid {alive()})")
    if os.fork():
        time.sleep(1.5)
        return print(f"started (pid {alive()})")
    os.setsid()
    if os.fork():
        os._exit(0)
    out = open(STATE / "scalp-daemon.log", "a")
    os.dup2(out.fileno(), 1)
    os.dup2(out.fileno(), 2)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    PID.write_text(str(os.getpid()))
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    run()


def stop():
    pid = alive()
    if not pid:
        return print("not running")
    os.kill(pid, signal.SIGTERM)
    print(f"stopped (pid {pid})")


def test():
    """Live plumbing check with 1 share: enter (long by default, `test short` for short), attach a far-away
    exit pair, then cancel and close. Refuses to run while a real position is open, since cleanup would close it."""
    if SYMBOL in positions():
        return print("a position is open; not testing")
    side = -1 if "short" in sys.argv else 1
    p = snapshots([SYMBOL])[SYMBOL]["price"]
    ok = enter(0, side, p, 40.0, 40.0, qty=1, suffix="-test")
    orders = api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=SYMBOL, nested="true"))
    print("entry + exit pair placed:", ok, "| resting:", [(o["type"], o["limit_price"], [(l["type"], l["stop_price"]) for l in o.get("legs") or []]) for o in orders])
    flatten("flat", "-test")
    time.sleep(3)
    print("after cleanup: position", positions().get(SYMBOL, {}).get("qty", 0), "| open orders", len(api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=SYMBOL))))


def twintest():
    """Live plumbing check for s6: buy 1 at-the-money call, then sell it."""
    spot = snapshots([SYMBOL])[SYMBOL]["price"]
    nxt = next(x[0] for x in sessions() if x[0] > race.today())
    sym = open_twin(0, 1, spot, nxt, contracts=1, suffix="-test")
    for _ in range(15):
        time.sleep(2)
        if sym in positions():
            break
    print("holding:", sym, positions().get(sym, {}).get("qty"), "@", positions().get(sym, {}).get("avg_entry_price"))
    close_twin(sym, 0, "-test")
    time.sleep(6)
    print("after cleanup:", "still held" if sym in positions() else "flat", "| open option orders:", len(api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=sym))))


if __name__ == "__main__":
    {"start": start, "stop": stop, "test": test, "twintest": twintest, "run": run}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
