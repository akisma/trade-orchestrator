#!/usr/bin/env python3
"""Paper-trading race: four mechanical strategies in one Alpaca PAPER account.

THROWAWAY SPIKE. Not the orchestrator, no tests, stdlib only. It can only ever talk to the
paper endpoint (hard-coded below) and refuses to start without paper keys.

  s1  Down ETFs all day        buy at the open, 5% stop, sell 5 min before the close
  s2  Short weak opens         at 10:00 short names whose first 30 min were red, cover before the close
  s3  Overnight rebound        before the close buy names down 3%+ on the day, sell at the next open
  s4  Overnight straddle       before the close buy an at-the-money call + put, sell after the next open
  s5  MU step-down scalp       runs separately in scalp.py (intraday, flat by the close)

Symbols never overlap between strategies, so positions cannot net against each other.
Every order carries client_order_id "race-<strategy>-<yyyymmdd>-<symbol>-<tag>", which also makes
re-runs idempotent (Alpaca rejects a duplicate id).

Usage: race.py start | stop | status | plan | run
"""
import json, os, re, signal, subprocess, sys, time, traceback, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STATE = HERE / "state"
TRADE = "https://paper-api.alpaca.markets/v2"  # paper only; there is no live URL anywhere in this file
DATA = "https://data.alpaca.markets"
RACE_START = "2026-11-25"

# dollars per position; shares are computed from the price at entry
S1 = {"SOXS": 14_400, "SQQQ": 27_500, "MUD": 33_100}
S1_STOP = 0.05
S2 = {"DELL": 17_300, "SNDK": 10_200, "AMAT": 14_500, "AAOI": 10_000, "BE": 11_500}  # MU belongs to s5 (scalp.py)
S3 = {"SOXL": 33_000, "MUU": 42_000}
S3_TRIGGER = -3.0  # percent, open → 5 min before the close
S4 = {"MU": 7_500, "DELL": 7_500}  # premium budget per underlying per night
STRATEGY_OF = {**{s: "s1" for s in S1}, **{s: "s2" for s in S2}, **{s: "s3" for s in S3}}
NAMES = {"s1": "Down ETFs all day", "s2": "Short weak opens at 10:00", "s3": "Overnight rebound", "s4": "Overnight straddle", "s5": "MU step-down scalp"}
DRY = False


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


ENV = load_env()
KEY, SEC = ENV.get("ALPACA_API_KEY", ""), ENV.get("ALPACA_SECRET_KEY", "")
if not KEY.startswith("PK") or ENV.get("ALPACA_PAPER_TRADE", "true").lower() != "true":
    sys.exit("race.py refuses to run: it needs Alpaca PAPER keys (PK...) and ALPACA_PAPER_TRADE=true")


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"{status} {body}")
        self.status, self.body = status, body


def api(method, url, params=None, body=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode()[:300])


def log(msg):
    STATE.mkdir(exist_ok=True)
    line = f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET  {msg}"
    print(line, flush=True)
    with open(STATE / "race.log", "a") as f:
        f.write(line + "\n")


def today():
    return datetime.now(ET).date()


def pct(a, b):
    return (a / b - 1) * 100


def snapshots(symbols):
    """Real-time snapshot on the free IEX feed. Price = last trade (the IEX bid/ask is not reliable)."""
    raw = api("GET", f"{DATA}/v2/stocks/snapshots", params=dict(symbols=",".join(symbols), feed="iex"))
    out = {}
    for s, sn in raw.items():
        bar = sn.get("dailyBar") or {}
        bar_day = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(ET).date() if bar.get("t") else None
        out[s] = dict(price=(sn.get("latestTrade") or {}).get("p"), open=bar.get("o"), open_is_today=bar_day == today())
    return out


def positions():
    return {p["symbol"]: p for p in api("GET", f"{TRADE}/positions")}


def submit(strategy, symbol, side, qty, tag, **extra):
    cid = f"race-{strategy}-{today():%Y%m%d}-{symbol}-{tag}"
    body = dict(symbol=symbol, qty=str(qty), side=side, type="market", time_in_force="day", client_order_id=cid, **extra)
    if DRY:
        log(f"  PLAN  {strategy} {side:4} {qty:>5} {symbol}  {extra or ''}")
        return None
    try:
        o = api("POST", f"{TRADE}/orders", body=body)
    except ApiError as e:
        log(f"  ORDER FAILED {cid}: {e}")
        return None
    log(f"  ORDER {cid}: {side} {qty} {symbol} → {o['status']}")
    return o


def flatten(strategy, symbols, tag):
    """Cancel resting orders (stops) on these symbols, then close whatever position is left."""
    pos = positions()
    held = [s for s in symbols if s in pos]
    for s in held:
        for o in api("GET", f"{TRADE}/orders", params=dict(status="open", symbols=s, nested="false")):
            if DRY:
                log(f"  PLAN  cancel resting {o['type']} order on {s}")
                continue
            try:
                api("DELETE", f"{TRADE}/orders/{o['id']}")
            except ApiError as e:
                log(f"  cancel failed on {s}: {e}")
    if held and not DRY:
        time.sleep(1.5)
    for s in held:
        qty = float(pos[s]["qty"])
        submit(strategy, s, "sell" if qty > 0 else "buy", int(abs(qty)), tag)
    for s in symbols:
        if s not in pos:
            log(f"  {strategy} {s}: no position to close")


# --- events ------------------------------------------------------------------------------------

def ev_exits_am(_next):
    log("EVENT morning exits (s3) + recovery check")
    leftovers = [s for s in list(S1) + list(S2) if s in positions()]
    if leftovers:
        log(f"  RECOVERY: {leftovers} were left open overnight (a close was missed); closing now")
        for s in leftovers:
            flatten(STRATEGY_OF[s], [s], "recover")
    flatten("s3", list(S3), "exit")


def ev_s1_entry(_next):
    log("EVENT s1 entry: buy down ETFs with a 5% stop")
    snap, pos = snapshots(list(S1)), positions()
    for s, dollars in S1.items():
        p = snap.get(s, {}).get("price")
        if not p or s in pos:
            log(f"  s1 {s}: skipped ({'already held' if s in pos else 'no price'})")
            continue
        submit("s1", s, "buy", int(dollars // p), "entry", order_class="oto", stop_loss={"stop_price": f"{p * (1 - S1_STOP):.2f}"})


def ev_options_exit(_next):
    log("EVENT s4 exit: sell straddles")
    for sym, p in positions().items():
        if p.get("asset_class") == "us_option":
            qty = float(p["qty"])
            submit("s4", sym, "sell" if qty > 0 else "buy", int(abs(qty)), "exit")


def ev_s2_entry(_next):
    log("EVENT s2 entry: short names whose first 30 minutes were red")
    snap, pos = snapshots(list(S2)), positions()
    for s, dollars in S2.items():
        sn = snap.get(s, {})
        if not sn.get("price") or not sn.get("open") or not (sn["open_is_today"] or DRY) or s in pos:
            log(f"  s2 {s}: skipped (no usable price/open, or already held)")
            continue
        f30 = pct(sn["price"], sn["open"])
        log(f"  s2 {s}: open {sn['open']} now {sn['price']} → first 30 min {f30:+.2f}%")
        if f30 < 0:
            submit("s2", s, "sell", int(dollars // sn["price"]), "entry")


def option_root(sym):
    m = re.match(r"^([A-Z.]+)\d{6}[CP]\d{8}$", sym)
    return m.group(1) if m else None


def s4_entry(next_session):
    snap = snapshots(list(S4))
    held = {option_root(s) for s, p in positions().items() if p.get("asset_class") == "us_option"}
    for u, budget in S4.items():
        spot = snap.get(u, {}).get("price")
        if not spot or u in held:
            log(f"  s4 {u}: skipped ({'already held' if u in held else 'no price'})")
            continue
        found, token = [], None
        while True:
            params = dict(underlying_symbols=u, status="active", limit=1000, strike_price_gte=round(spot * 0.9, 2), strike_price_lte=round(spot * 1.1, 2),
                          expiration_date_gte=(next_session + timedelta(days=1)).isoformat(), expiration_date_lte=(next_session + timedelta(days=14)).isoformat())
            if token:
                params["page_token"] = token
            r = api("GET", f"{TRADE}/options/contracts", params=params)
            found += r.get("option_contracts", [])
            token = r.get("next_page_token")
            if not token:
                break
        if not found:
            log(f"  s4 {u}: no contracts found")
            continue
        exp = min(c["expiration_date"] for c in found)
        by_strike = {}
        for c in found:
            if c["expiration_date"] == exp:
                by_strike.setdefault(float(c["strike_price"]), {})[c["type"]] = c["symbol"]
        strike = min((k for k, v in by_strike.items() if len(v) == 2), key=lambda k: abs(k - spot))
        call, put = by_strike[strike]["call"], by_strike[strike]["put"]
        q = api("GET", f"{DATA}/v1beta1/options/snapshots", params=dict(symbols=f"{call},{put}", feed="indicative")).get("snapshots", {})
        ask = lambda s: (q.get(s, {}).get("latestQuote") or {}).get("ap") or (q.get(s, {}).get("latestTrade") or {}).get("p")
        if not ask(call) or not ask(put):
            log(f"  s4 {u}: no option price for {call}/{put}, skipped")
            continue
        cost = (ask(call) + ask(put)) * 100
        n = int(budget // cost) or (1 if cost <= budget * 1.6 else 0)
        log(f"  s4 {u}: spot {spot}, {exp} strike {strike:g}, straddle ≈ ${cost:,.0f} → {n} of each")
        if n:
            submit("s4", call, "buy", n, "entry")
            submit("s4", put, "buy", n, "entry")


def ev_close(next_session):
    log("EVENT close: exit s1 + s2, then overnight entries s3 + s4")
    flatten("s1", list(S1), "exit")
    flatten("s2", list(S2), "exit")
    snap, pos = snapshots(list(S3)), positions()
    for s, dollars in S3.items():
        sn = snap.get(s, {})
        if not sn.get("price") or not sn.get("open") or not (sn["open_is_today"] or DRY) or s in pos:
            log(f"  s3 {s}: skipped (no usable price/open, or already held)")
            continue
        day = pct(sn["price"], sn["open"])
        log(f"  s3 {s}: open {sn['open']} now {sn['price']} → day {day:+.2f}% (buys at {S3_TRIGGER}% or worse)")
        if day <= S3_TRIGGER:
            submit("s3", s, "buy", int(dollars // sn["price"]), "entry")
    s4_entry(next_session)


# --- scheduler ---------------------------------------------------------------------------------

_SESSIONS = (None, None)  # (fetched for date, sessions); the calendar only needs one lookup per day


def sessions():
    global _SESSIONS
    d0 = today()
    if _SESSIONS[0] != d0:
        cal = api("GET", f"{TRADE}/calendar", params=dict(start=d0.isoformat(), end=(d0 + timedelta(days=10)).isoformat()))
        at = lambda d, hm: datetime.combine(d, dtime(*map(int, hm.split(":"))), ET)
        _SESSIONS = (d0, [(date.fromisoformat(c["date"]), at(date.fromisoformat(c["date"]), c["open"]), at(date.fromisoformat(c["date"]), c["close"])) for c in cal])
    return _SESSIONS[1]


def schedule(op, cl):
    """(name, fire at, give up after, handler). Exits get long grace; entries are skipped if late."""
    s, m = lambda n: timedelta(seconds=n), lambda n: timedelta(minutes=n)
    return [("exits_am", op + s(15), cl - m(10), ev_exits_am), ("s1_entry", op + s(20), op + m(10), ev_s1_entry),
            ("options_exit", op + m(5), cl - m(10), ev_options_exit), ("s2_entry", op + m(30) + s(5), op + m(35), ev_s2_entry),
            ("close", cl - m(5), cl - s(20), ev_close)]


def run():
    done_file = STATE / "done.json"
    done = set(json.loads(done_file.read_text())) if done_file.exists() else set()
    log(f"runner started (pid {os.getpid()})")
    while True:
        now = datetime.now(ET)
        try:
            ses = sessions()
        except Exception as e:
            log(f"calendar lookup failed: {e}")
            time.sleep(60)
            continue
        sess = next((x for x in ses if x[0] == now.date()), None)
        pending = [e for e in schedule(sess[1], sess[2]) if f"{now.date()}:{e[0]}" not in done] if sess else []
        if not pending:
            time.sleep(30)
            continue
        name, at, deadline, handler = pending[0]
        if now < at:
            time.sleep(min(20, (at - now).total_seconds()))
            continue
        done.add(f"{now.date()}:{name}")
        done_file.write_text(json.dumps(sorted(done)))
        if now > deadline:
            log(f"MISSED {name} (due {at:%H:%M:%S}, woke at {now:%H:%M:%S}); skipped")
            continue
        try:
            handler(next(x[0] for x in ses if x[0] > now.date()))
        except Exception:
            log(f"EVENT {name} FAILED:\n{traceback.format_exc()}")


def pid_alive():
    f = STATE / "race.pid"
    if not f.exists():
        return None
    pid = int(f.read_text())
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def start():
    STATE.mkdir(exist_ok=True)
    if pid_alive():
        return print(f"already running (pid {pid_alive()})")
    if os.fork():
        time.sleep(1.5)
        return print(f"started (pid {pid_alive()}); log: {STATE / 'race.log'}")
    os.setsid()
    if os.fork():
        os._exit(0)
    out = open(STATE / "daemon.log", "a")
    os.dup2(out.fileno(), 1)
    os.dup2(out.fileno(), 2)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    (STATE / "race.pid").write_text(str(os.getpid()))
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])  # no idle sleep while we run
    run()


def stop():
    pid = pid_alive()
    if not pid:
        return print("not running")
    os.kill(pid, signal.SIGTERM)
    print(f"stopped (pid {pid})")


def status():
    pid = pid_alive()
    print(f"runner: {'running, pid ' + str(pid) if pid else 'NOT RUNNING'}")
    try:
        scalp_pid = int((STATE / "scalp.pid").read_text())
        os.kill(scalp_pid, 0)
        print(f"scalp runner (s5): running, pid {scalp_pid}")
    except (OSError, ValueError):
        print("scalp runner (s5): NOT RUNNING (start with: python3 spikes/paper-race/scalp.py start)")
    orders, after = [], f"{RACE_START}T00:00:00Z"
    while True:
        page = api("GET", f"{TRADE}/orders", params=dict(status="all", limit=500, after=after, direction="asc", nested="true"))
        orders += page
        if len(page) < 500:
            break
        after = page[-1]["submitted_at"]
    pos = positions()
    book = {}  # (strategy, symbol) -> [cash, net qty, fills]
    for o in orders:
        cid = o.get("client_order_id", "")
        if not cid.startswith("race-") or "-test" in cid:
            continue
        strat = cid.split("-")[1]
        for f in [o] + (o.get("legs") or []):
            qty = float(f.get("filled_qty") or 0)
            if not qty:
                continue
            mult = 100 if f.get("asset_class") == "us_option" else 1
            sign = 1 if f["side"] == "buy" else -1
            b = book.setdefault((strat, f["symbol"]), [0.0, 0.0, 0])
            b[0] -= sign * qty * float(f["filled_avg_price"]) * mult
            b[1] += sign * qty
            b[2] += 1
    totals = {}
    for (strat, sym), (cash, net, fills) in sorted(book.items()):
        mult = 100 if option_root(sym) else 1
        mark = net * float(pos[sym]["current_price"]) * mult if abs(net) > 1e-9 and sym in pos else 0.0
        t = totals.setdefault(strat, [0.0, 0, []])
        t[0] += cash + mark
        t[1] += fills
        t[2].append(f"{sym} {cash + mark:+,.0f}" + (f" (open {net:g})" if abs(net) > 1e-9 else ""))
    print(f"\n{'strategy':30} {'P&L $':>10} {'fills':>6}  by symbol")
    for strat in NAMES:
        pl, fills, detail = totals.get(strat, [0.0, 0, []])
        print(f"{strat} {NAMES[strat]:27} {pl:>10,.0f} {fills:>6}  {', '.join(detail)}")
    acct = api("GET", f"{TRADE}/account")
    print(f"\naccount equity ${float(acct['equity']):,.2f}; open positions: {len(pos)}")


def plan():
    """Dry run of every event with current prices. Sends nothing."""
    global DRY
    DRY = True
    ses = sessions()
    nxt = next(x[0] for x in ses if x[0] > today())
    for fn in (ev_exits_am, ev_s1_entry, ev_options_exit, ev_s2_entry, ev_close):
        fn(nxt)
    print("\nupcoming sessions:", [(d.isoformat(), f"{o:%H:%M}", f"{c:%H:%M}") for d, o, c in ses[:5]])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": start, "stop": stop, "status": status, "plan": plan, "run": run}.get(cmd, lambda: print(__doc__))()
