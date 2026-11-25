# Paper race (throwaway spike)

Four mechanical strategies racing in one Alpaca **paper** account, to see how each behaves in live
market conditions. This is an experiment harness, not the orchestrator: no tests, no risk layer,
stdlib only. `race.py` hard-codes the paper endpoint and refuses to start without paper keys.

Started 2026-11-25; planned to run through Fri 2026-12-04 (7 sessions; Nov 27 closes at 1pm ET).

## The strategies

| | Rule | Picks ($ per position) |
|---|---|---|
| s1 Down ETFs all day | Buy at the open with a 5% stop; sell 5 min before the close | SOXS 14.4k, SQQQ 27.5k, MUD 33.1k |
| s2 Short weak opens | At 10:00 short names whose first 30 min were red; cover 5 min before the close | DELL 17.3k, SNDK 10.2k, AMAT 14.5k, AAOI 10k, MU 11.5k, BE 11.5k |
| s3 Overnight rebound | 5 min before the close, buy names down 3%+ since the open; sell at the next open | SOXL 33k, MUU 42k |
| s4 Overnight straddle | 5 min before the close, buy an at-the-money call + put (nearest expiry after the next session); sell 5 min after the next open | MU, DELL: $7.5k premium each |

Each strategy has about $75k (s4 risks premium only). Positions inside a strategy are sized by
inverse volatility. Picks come from a hindsight study of Oct 13 – Nov 24 2026, a single crash
regime, so they are flattered. NVDA and anything driven by it directly (NVDQ) is excluded as an
anomaly; cybersecurity names are excluded as too quiet.

## Running it

```
python3 spikes/paper-race/race.py start    # detach and run in the background
python3 spikes/paper-race/race.py status   # runner state + P&L per strategy
python3 spikes/paper-race/race.py plan     # dry run of every event, sends nothing
python3 spikes/paper-race/race.py stop
```

Log: `spikes/paper-race/state/race.log` (gitignored).

## Known limits

- The runner is a background process. It does not survive a reboot or logout; run `start` again.
- The Mac must be awake at the open, 10:00 and 5 min before the close (ET). `caffeinate` blocks
  idle sleep only; a closed lid still sleeps. A missed entry is skipped; a missed exit is closed
  at the next open and logged as RECOVERY.
- Real-time prices come from the free IEX feed (last trade), which can differ slightly from the
  consolidated price. Fine for deciding red/green, not for pricing limit orders.
- Paper fills ignore liquidity and spreads, so results are optimistic, especially for options and MUD.
- A few sessions cannot separate skill from luck. Read the result as "what happened", not proof.
