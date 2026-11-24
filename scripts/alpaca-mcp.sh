#!/usr/bin/env bash
# Launch Alpaca's official MCP server with credentials from .env (gitignored).
# Paper trading is the default; ALPACA_PAPER_TRADE=false in .env is the only way to go live.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo "trade-orchestrator: .env not found — copy .env.example and add your Alpaca paper keys" >&2
  exit 1
fi
set -a; source .env; set +a
: "${ALPACA_API_KEY:?ALPACA_API_KEY missing from .env}"
: "${ALPACA_SECRET_KEY:?ALPACA_SECRET_KEY missing from .env}"
export ALPACA_PAPER_TRADE="${ALPACA_PAPER_TRADE:-true}"
exec uvx alpaca-mcp-server
