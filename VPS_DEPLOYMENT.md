markdown
# Zephyros — Complete Project Reference

> Single source of truth for the L2 trading bot: architecture,
> configuration, deployment, and operations.

---

## Table of Contents

1. [What This Is](#1-what-this-is)
2. [Architecture](#2-architecture)
3. [File Layout](#3-file-layout)
4. [Configuration](#4-configuration)
5. [Data Pipeline](#5-data-pipeline)
6. [Regime Filter](#6-regime-filter)
7. [Strategy — absorption_zones](#7-strategy--absorption_zones)
8. [Execution — MT5](#8-execution--mt5)
9. [Risk Management](#9-risk-management)
10. [UI](#10-ui)
11. [Running Locally](#11-running-locally)
12. [VPS Deployment](#12-vps-deployment)
13. [Troubleshooting](#13-troubleshooting)
14. [Quick Reference](#14-quick-reference)
15. [Changelog](#15-changelog)

---

## 1. What This Is

A crypto trading bot that:

1. **Listens** to Binance live order book (L2) + tape (aggTrades)
2. **Classifies** market regime from live L2 microstructure
   (CVD + OBI + BVI over 5-minute rolling window)
3. **Detects** breakout setups when a liquidity wall decays
   (front break) or a price void opens (void break)
4. **Routes** approved signals to MT5 (Exness / XM360)
5. **Records** L2 history to SQLite for warm-up on restart

**Designed for:** continuous operation with graceful restart.
**Not designed for:** latency arbitrage, HFT, or pairs trading.

---

## 2. Architecture
┌──────────────────────────────────────────────────────────┐
│ BINANCE │
│ ws: depth @100ms + aggTrade │
└──────────────────────────┬───────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────┐
│ FEEDS │
│ feed.binance ──► feed.depth / feed.trade │
│ feed.klines ──► feed.kline │
└──────────────────────────┬───────────────────────────────┘
│
┌──────────────────┼───────────────────┐
▼ ▼ ▼
┌───────────────┐ ┌──────────────┐ ┌──────────────────┐
│ L2 Recorder │ │ RegimeEngine │ │ StrategyManager │
│ (local mode) │ │ │ │ │
│ ↓ │ │ CVD+OBI+BVI │ │ absorption_zones │
│ SQLite │ │ over 300s │ │ │
│ l2_history.db │ │ ↓ │ │ ↓ │
│ │ │ regime_update│ │ signal_proposed │
└───────────────┘ └──────────────┘ └────────┬─────────┘
│
▼
┌──────────────────┐
│ RiskManager │
│ daily loss cap │
│ pyramiding check │
│ cooldowns │
└────────┬─────────┘
│
▼
┌──────────────────┐
│ MT5Executor │
│ Exness / XM360 │
└──────────────────┘

text

**Event Bus** (`app/core/event_bus.py`) is the spine —
all modules communicate via pub/sub, zero direct coupling.

**Failure isolation:** if MT5 disconnects, feeds + strategy keep
running. If feeds drop, MT5 stays connected. Each module boots
independently in `main.py`.

---

## 3. File Layout
Zephyros/
├── app/
│ ├── main.py # Bootloader (qasync + modules)
│ ├── core/
│ │ ├── config.py # Pydantic config
│ │ ├── data_store.py # SQLite L2 + VPS reader
│ │ ├── database.py # Trades/signals SQLite
│ │ ├── event_bus.py # Async pub/sub
│ │ ├── module.py # BaseModule ABC
│ │ ├── symbols.py # Symbol registry
│ │ └── symbols_persist.py # Symbol override file
│ ├── modules/
│ │ ├── aggregator/
│ │ │ ├── book_broadcaster.py
│ │ │ ├── status_relay.py
│ │ │ └── tape_aggregator.py
│ │ ├── analytics/
│ │ │ └── regime_engine.py # L2 regime classifier
│ │ ├── data/
│ │ │ └── recorder.py # L2Recorder
│ │ ├── execution/
│ │ │ └── mt5_executor.py
│ │ ├── feed_binance/
│ │ │ ├── feed.py # Depth + aggTrade
│ │ │ └── klines.py # Multi-interval klines
│ │ ├── history/
│ │ │ └── zones_history.py # HTF pivots
│ │ ├── mock/
│ │ │ └── feed.py
│ │ ├── risk/
│ │ │ └── manager.py
│ │ └── strategy/
│ │ ├── base.py # Signal, Strategy ABC
│ │ ├── registry.py
│ │ ├── manager.py
│ │ ├── absorption_zones.py # Main strategy
│ │ ├── imbalance.py # Alt strategy (unused)
│ │ └── voids.py # Void primitives
│ └── ui/
│ ├── main_window.py
│ ├── base_widget.py # Panel (glassmorphic)
│ ├── ribbon.py # Top bar w/ pills
│ ├── sidebar.py
│ ├── theme.py
│ ├── dialogs/candlestick.py
│ ├── models/ # Table models
│ ├── pages/ # Dashboard, Chart, Risk, Symbols
│ └── widgets/ # Orderbook, Tape, DepthChart...
├── config/
│ ├── default.yaml # Committed defaults
│ ├── local.yaml # Gitignored (API keys, mode overrides)
│ └── symbols.local.yaml # Gitignored (symbol overrides)
├── data/
│ ├── trades.db # Trade history
│ ├── risk_state.json
│ └── l2_history.db # L2 snapshots (local mode)
├── logs/
├── scripts/
│ ├── mt5_smoke.py
│ └── test_*.py
├── tests/
├── vps_server.py # Standalone VPS recorder
├── requirements.txt # Local bot deps
├── requirements-server.txt # VPS deps
├── ZEPHYROS.md # This file
├── run.bat
└── .env # Optional MT5 credentials

text

---

## 4. Configuration

Layered, high-to-low precedence:
config/default.yaml (committed)
↓ overridden by
config/{env}.yaml (dev/staging/prod)
↓ overridden by
config/local.yaml (gitignored)
↓ overridden by
.env + env vars (BOT_*)

text

### Key sections

#### `app`
```yaml
app:
  name: "L2 Trading Bot"
  env: "dev"                    # dev | staging | prod
  log_level: "INFO"             # DEBUG | INFO | WARNING | ERROR
  database_path: "data/trades.db"
  risk_state_path: "data/risk_state.json"
  dev_mode: true                # shows "Test Trade" menu
data_pipeline
yaml
data_pipeline:
  storage_mode: "local"         # local | vps | disabled
  local:
    db_path: "data/l2_history.db"
    retention_hours: 24
    flush_interval_sec: 5
    janitor_interval_sec: 300
    warmup_seconds: 300
  vps:
    api_url: "http://YOUR_VPS_IP:8000"
    api_key: "CHANGE_ME"
    warmup_seconds: 300
Modes:

Mode	Bot writes	Bot reads	VPS writes
local	✅ SQLite	✅ SQLite	—
vps	❌	✅ HTTP GET	✅ own Binance
disabled	❌	❌	—
regime
yaml
regime:
  enabled: true
  timeframe: "5m"               # klines interval for ATR only
  lookback_bars: 100
  window_seconds: 300           # L2 rolling window
  min_samples: 120              # warmup threshold
  book_levels: 10               # depth levels for OBI/BVI
  cvd_threshold: 0.15
  obi_threshold: 0.15
  bvi_threshold: 0.40
  atr_period: 14
  volatility_atr_pct_threshold: 0.20
strategies.absorption_zones
Most-tuned knobs:

yaml
zone_bin_size_pct: 0.012        # bin width as % of price
zone_half_life_sec: 45          # L2 profile decay
min_wall_qty: 3.0               # minimum wall to consider
stability_min_age_ms: 30000     # zone must live 30s
depletion_pct_min: 0.50         # wall 50% gone → void break
front_break_depletion_min: 0.60 # near edge 60% gone → front break
directional_flow_min: 0.55      # CVD must agree
execution.mt5
yaml
execution:
  mt5:
    enabled: true
    magic: 20251016              # our order tag
    deviation_points: 20
    filling_mode: "IOC"          # IOC | FOK | RETURN
    order_timeout_ms: 10000
risk
yaml
risk:
  max_daily_loss_pct: 2.00
  max_open_positions: 3
  no_pyramiding: true
  cooldown_after_exit_sec: 5
  cooldown_after_loss_sec: 60
  persist_state: true            # survives restart
5. Data Pipeline
5.1 Live path (always active)
The bot's own Binance WebSocket connection feeds everything in real
time — strategy, regime, UI. This never stops, regardless of
storage_mode.

5.2 Persistence path
local mode:

L2Recorder subscribes to feed.depth + feed.trade

Batches to memory, flushes every 5s to data/l2_history.db

Janitor deletes rows older than retention_hours

vps mode:

L2Recorder is a no-op (logs "recorder idle")

VPS records 24/7 into its own vps_l2_history.db

Bot reads via HTTP GET only

5.3 Schema
sql
CREATE TABLE l2_snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    bids    TEXT NOT NULL,        -- str([[price, qty], ...])
    asks    TEXT NOT NULL
);
CREATE INDEX idx_l2_symbol_ts ON l2_snapshots(symbol, ts);

CREATE TABLE l2_trades (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    price   REAL NOT NULL,
    qty     REAL NOT NULL,
    side    TEXT NOT NULL
);
CREATE INDEX idx_l2t_symbol_ts ON l2_trades(symbol, ts);
5.4 Warm-up on START
When user clicks START, StrategyManager calls
absorption_zones.initialize():

Query get_latest_timestamp(symbol)

If gap > 24h or no data → fall back to 15-min live warmup

Else fetch get_recent_snapshots(limit=3000) +
get_recent_trades(since_ts=now-300s)

Replay into memory:

Snapshots → _update_profiles, _record_bid_stack, _record_mid

Final snapshot → rebuild zones + voids

Trades → _trades deque for CVD

_warmup_until_ts = 0 → immediately tradeable

Same happens on bot startup for RegimeEngine — replays
snapshots+trades so the first regime publish is accurate.

6. Regime Filter
6.1 The three L2 concepts
Metric	Measures	Formula
CVD	Aggressive flow direction	Σ(buy_qty) - Σ(sell_qty) / Σ(all_qty)
OBI	Resting book imbalance	(bid_qty - ask_qty) / (bid_qty + ask_qty) over top N
BVI	Book velocity / shift	tanh((bid_now/median_bid - 1) - (ask_now/median_ask - 1))
All three sample over a 300-second rolling window. Values are
normalized to [-1, +1].

6.2 Classification
text
STRONG_BULLISH    CVD+ AND OBI+ AND BVI+    → BUY only
BULLISH           CVD+ AND (OBI+ OR BVI+)   → BUY only
NEUTRAL           mixed signals              → block all
BEARISH           CVD- AND (OBI- OR BVI-)   → SELL only
STRONG_BEARISH    CVD- AND OBI- AND BVI-    → SELL only
DIVERGENT_BULL    CVD+ BUT OBI- AND BVI-    → block (fake pump)
DIVERGENT_BEAR    CVD- BUT OBI+ AND BVI+    → block (fake dump)
VOLATILE          ATR% > threshold           → block all
WARMING_UP        < min_samples               → block all
6.3 How strategy uses it
Entry filter — in _maybe_void_break, _maybe_front_break,
_maybe_void_collapse:

python
if not self._regime_allows(action):
    return None

def _regime_allows(action):
    if action == "BUY":
        return regime in ("BULLISH", "STRONG_BULLISH")
    if action == "SELL":
        return regime in ("BEARISH", "STRONG_BEARISH")
    return False
Exit filter — in _maybe_exit_long / _maybe_exit_short:

python
def _check_regime_exit(side):
    if side == "long" and regime in (
        "BEARISH", "STRONG_BEARISH", "DIVERGENT_BEARISH", "VOLATILE"
    ):
        return close_signal
    if side == "short" and regime in (
        "BULLISH", "STRONG_BULLISH", "DIVERGENT_BULLISH", "VOLATILE"
    ):
        return close_signal
    return None
Regime flips inside 300s are ignored — we only enter sustainable
regimes, and only exit when the regime inverts against the position.

7. Strategy — absorption_zones
7.1 What it does
Reconstructs live liquidity zones from L2 book. Trades breakouts
of decaying walls (no mean reversion — that was removed).

Signal types:

Type	Trigger	Example
Void break	Wall decays to < 50%, void opens behind it	Wall at 82,000 fades, empty to 82,500 → SELL
Front break	Near edge of zone fades past 60% with CVD confirmation	Demand at 81,900 front edge depletes → SELL
Void collapse	Void boundary decaying + directional context	Price falling, bid void opens below → SELL
7.2 Zone lifecycle
Profile tracking — bids/asks aggregated into price bins
(bin_size_pct of mid)

Extraction — bins with score > threshold cluster into zones

Tracking — zones age, get near_edge/far_edge Front
objects that track depletion and decay

Break detection — when near_edge.depletion > 60% or
far_edge.depletion > 50%, check for signal

Execution — if regime agrees, publish signal → risk → MT5

7.3 Exit conditions
Structural exits (priority 4):

Zone broken (price through opposite edge)

Far edge depleted

Wall replenished

Target reached

Hard stop (front_break_stop_buffer, stop_buffer)

Time stop (90s front, 60s void, 120s collapse)

Regime exit (priority 3 — before structural):

Regime flipped to opposing direction

Regime became VOLATILE

Order of evaluation in _maybe_exit_long:

text
1. Hydrate grace check
2. REGIME EXIT           ← new
3. Style-specific exits
8. Execution — MT5
8.1 Connection
text
_connect() → mt5.initialize()
          → mt5.login() if credentials set
          → mt5.account_info() sanity check
All MT5 calls run on a dedicated single-thread worker because
MT5's Python bindings are thread-affine.

8.2 Order flow
text
signal_approved → _on_signal()
                → _ensure_symbol_visible()
                → _detect_filling()      (broker-specific)
                → order_send(market)
                → subscribe tick updates
                → track in self._positions
                → watch for SL hit / manual close
8.3 Reconciliation on startup
If bot restarts with positions open at broker:

mt5.positions_get() filtered by magic

Any broker position not in DB → insert as orphaned

Any DB trade not at broker → close from history or mark lost

Prevents phantom positions after a restart.

8.4 Keepalive discipline
MT5 desktop app must stay running. See §13.2 Sleep/Offline for
the full fix list.

9. Risk Management
9.1 Gates (evaluated in order)
Halted? — daily loss cap breached → reject

Max positions? — len(open_positions) >= max_open → reject

Pyramiding? — already in symbol → reject (if no_pyramiding)

Post-exit cooldown — cooldown_after_exit_sec

Post-loss cooldown — cooldown_after_loss_sec

Signal age — max_signal_age_sec (default 5s)

9.2 Daily P&L tracking
On every trade_closed event:

python
daily_realized_pnl += pnl
daily_trades += 1
if pnl > 0: daily_wins += 1
if pnl < 0: daily_losses += 1
if -daily_realized_pnl / starting_equity * 100 >= max_daily_loss_pct:
    trading_halted = True
    halt_reason = "daily loss cap"
starting_equity is read from mt5.account_info() at first
account_update event. Persisted to risk_state.json, survives
restart.

9.3 Day rollover
daily_reset_hour_utc (default 0 = midnight UTC). At rollover:

Reset daily_realized_pnl, counts, cooldowns

Clear trading_halted

Re-capture starting_equity

10. UI
10.1 Layout
text
┌──────────────────────────────────────────────────────────┐
│ SIDEBAR  │  RIBBON (page title + trading + pills)        │
│          ├──────────────────────────────────────────────┤
│ ▦ Telemetry   [Symbol][Lot][START][⟳]                    │
│ ▲ Chart       REGIME: BULLISH  SIGNAL: WATCHING  ● LIVE  │
│ ◆ Risk        ──────────────────────────────────────────  │
│ ◇ Symbols     PAGE CONTENT (grid / chart / etc.)         │
│ ⚙ System                                                  │
│               ──────────────────────────────────────────  │
│               EVENT STREAM (collapsible)                 │
└──────────────────────────────────────────────────────────┘
10.2 Ribbon pills
Pill	Colors	Purpose
REGIME	green bull, red bear, amber volatile, gray neutral	Live regime
SIGNAL	green long, red short, amber watching, gray idle/flat	Strategy state
STATUS	green LIVE, amber FEED, red OFFLINE	Connections
Hover for breakdown (CVD / OBI / BVI for regime; side/peak for signal).

10.3 Panels
Dashboard: OrderBook, Tape, DepthChart, Signals, Positions

Chart: Candlestick + HTF zones overlay

Risk: Account, Trade History, Rejections

Symbols: Editable registry (persists to symbols.local.yaml)

11. Running Locally
11.1 First run
bash
# Install deps
pip install -r requirements.txt

# Configure MT5 credentials (optional)
# Either config/local.yaml → execution.mt5.login/password/server
# Or .env → BOT_MT5_LOGIN / BOT_MT5_PASSWORD / BOT_MT5_SERVER

# Start
python -m app.main
11.2 Startup sequence
text
1. Load config          (default.yaml → env.yaml → local.yaml → env vars)
2. Init SQLite          (data/trades.db, data/l2_history.db)
3. Boot MODULES         (in order from app/main.py)
   ├── BinanceFeed      (WS connect)
   ├── KlineFeed        (WS connect, all intervals)
   ├── HistoryZones     (fetch 10 TFs from REST)
   ├── L2Recorder       (subscribe or idle based on storage_mode)
   ├── RegimeEngine     (subscribe + warm from DB)
   ├── BookBroadcaster  (throttle to UI)
   ├── TapeAggregator   (batch trades)
   ├── StatusRelay      (heartbeat)
   ├── StrategyManager  (load active strategy)
   ├── RiskManager      (load state, daily rollover)
   └── MT5Executor      (connect, reconcile, subscribe)
4. Show UI              (loads pages from PAGE_REGISTRY)
5. User clicks START    → strategy.initialize() → warmed from DB
11.3 Local warm-up logs
Expected within 10 seconds of clicking START:

text
Warming up BTCUSDT from DB: 3000 snapshots, 8000 trades
Warm-up complete in 1.2s — profiles=175/180 zones=2/3 voids=1/2 trades=8000
Or, if DB has no data:

text
L2 gap > 24h or no data — 15 min warm-up for BTCUSDT
Then trades start after 15 minutes of live observation.

12. VPS Deployment
Purpose: Record L2 data 24/7 on a remote server so the bot can
warm up instantly regardless of PC uptime. Eliminates the "no data
because PC was off" failure mode.

12.1 Prerequisites
Item	Minimum	Recommended
VPS	1 vCPU / 512MB / 20GB	2 vCPU / 1GB / 40GB
OS	Ubuntu 20.04	Ubuntu 22.04 LTS
Cost	$4/mo	$6–10/mo
Region	Any	Tokyo / Frankfurt / Singapore
Region matters: VPS → Binance latency. Tokyo is 5–15ms.
US regions are geo-blocked by Binance — do not use.

12.2 Files to deploy
text
scp vps_server.py requirements-server.txt VPS_GUIDE.md \
    root@YOUR_VPS_IP:~/zephyros-vps/
12.3 Setup
On the VPS:

bash
# 1. System packages
apt update && apt install -y python3 python3-pip python3-venv ufw

# 2. Python venv
cd ~/zephyros-vps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-server.txt

# 3. Edit API key (line ~20 of vps_server.py)
#   API_KEY = "YOUR_STRONG_32_CHAR_KEY"

# 4. Firewall — SSH FIRST or you lock yourself out
ufw allow 22/tcp
ufw allow 8000/tcp
ufw enable
ufw status

# 5. Run once to verify
python vps_server.py
# Ctrl+C after confirming snapshots appear
12.4 Systemd service
/etc/systemd/system/zephyros-vps.service:

ini
[Unit]
Description=Zephyros VPS L2 Recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/zephyros-vps
ExecStart=/root/zephyros-vps/venv/bin/python /root/zephyros-vps/vps_server.py
Restart=always
RestartSec=10
StandardOutput=append:/root/zephyros-vps/vps_recorder.log
StandardError=append:/root/zephyros-vps/vps_recorder.log

[Install]
WantedBy=multi-user.target
Enable:

bash
systemctl daemon-reload
systemctl enable zephyros-vps
systemctl start zephyros-vps
systemctl status zephyros-vps
12.5 Verify
bash
# On VPS
tail -f ~/zephyros-vps/vps_recorder.log
# Expect: [flush] 200 snapshots, 500 trades every 5s

# From any machine
curl "http://YOUR_VPS_IP:8000/health"
# {"status":"ok","ts":...}

curl -H "X-API-Key: YOUR_KEY" "http://YOUR_VPS_IP:8000/stats"
# {"snapshots":[{"symbol":"BTCUSDT","rows":300,...}],...}
12.6 Bot configuration
config/local.yaml on the bot PC:

yaml
data_pipeline:
  storage_mode: "vps"
  vps:
    api_url: "http://YOUR_VPS_IP:8000"
    api_key: "YOUR_STRONG_32_CHAR_KEY"
    warmup_seconds: 300
Restart bot. Expect:

text
L2 pipeline: VPS mode (bot reads, VPS records)
L2 recorder idle — storage_mode='vps'. VPS handles its own recording.
Regime warm-up: replayed 3000 snapshots, 15000 trades from DB
12.7 Operating commands
Action	Command
Status	systemctl status zephyros-vps
Restart	systemctl restart zephyros-vps
Live logs	tail -f ~/zephyros-vps/vps_recorder.log
Disk usage	df -h && du -sh ~/zephyros-vps/*.db*
Row counts	sqlite3 ~/zephyros-vps/vps_l2_history.db "SELECT symbol, COUNT(*) FROM l2_snapshots GROUP BY symbol;"
Lag check	sqlite3 ~/zephyros-vps/vps_l2_history.db "SELECT (strftime('%s','now')*1000 - MAX(ts)) FROM l2_snapshots;"
Disk growth: ~50 MB/hour, stabilizes at ~1.2 GB after 24h retention.

12.8 Updating the VPS code
If vps_server.py changes:

bash
scp vps_server.py root@VPS:~/zephyros-vps/vps_server.py.new
ssh root@VPS "
  cd ~/zephyros-vps
  mv vps_server.py.new vps_server.py
  systemctl restart zephyros-vps
  systemctl status zephyros-vps
"
SQLite file is untouched — data survives updates.

13. Troubleshooting
13.1 Freeze / UI unresponsive
Symptom: clicking buttons hangs, UI goes gray.

Cause: Synchronous I/O on the main thread (usually large SQLite
reads, or writing 1GB+ parquet files).

Check: data/l2_history.db size.

< 100 MB → fine

500 MB → janitor should have trimmed it

Fix:

bash
systemctl restart bot  # or Ctrl+C and rerun
For trade history freezes: the current risk_page.py uses
asyncio.to_thread + debounced refresh — shouldn't happen. If it
does, ROW_LIMIT is probably too high (default 500).

13.2 MT5 disconnects / goes offline
Symptom: keepalive ping timeout in logs, or IPC timeout -10005.

Cause: Windows sleeping the app, or the machine going offline.

Fix checklist:

Power settings: Control Panel → Power Options → High Performance →
"Put the computer to sleep" = Never

Network adapter: Device Manager → NIC → Properties → Power
Management → uncheck "Allow the computer to turn off..."

RDP: always Disconnect (X button), never Sign Out

Windows Update: set active hours, disable auto-restart

Hibernate: powercfg /hibernate off

Watchdog script (runs every 5 min via Task Scheduler):

powershell
if (!(Get-Process terminal64 -ErrorAction SilentlyContinue)) {
    Start-Process "C:\Program Files\MetaTrader 5\terminal64.exe"
}
13.3 keepalive ping timeout from Binance
Symptom: recurring WS reconnects every 50 seconds.

Cause: Event loop blocked for 30+ seconds. Usually disk I/O from
L2 recorder.

Fix: verify you're on SQLite backend (data/l2_history.db
exists, not .parquet files). If parquet still present, migrate per
§5.4.

Confirm — logs should show:

text
L2 pipeline: LOCAL mode (bot reads and records)
(not "Using LOCAL L2 Store" from old parquet code)

13.4 Regime stuck at — for > 2 minutes
Cause: RegimeEngine not running, or no samples received.

Fix:

Check log for RegimeEngine (L2) started — window=300s min_samples=120

If missing → main.py doesn't have RegimeEngine() in MODULES

If shows RegimeEngine started (TF: 5m) → old klines-based
version, replace regime_engine.py

Check 📊 Regime: WARMING_UP (45/120 samples) — if samples
aren't increasing, feed.depth isn't firing → check BookBroadcaster

13.5 No signals firing
Symptom: bot running, regime valid, but no trades for hours.

Cause: cascade of strict filters. Debug step-by-step:

Is regime in the right state? For BUY, need BULLISH or
STRONG_BULLISH. If regime keeps flipping to NEUTRAL /
DIVERGENT_*, strategy correctly blocks.

Are zones valid? Check dump_zones output (auto-log every
5 min). Look for [OK] zones, not [XX].

Is min_wall_qty too high? Default 3.0 BTC. Try 1.5 if
market is thin.

Is stability_min_age_ms too long? Default 30s. Try 15s.

Is directional_flow_min too high? Default 0.55. Try 0.45.

Is HTF clearance blocking? Should be use_htf_clearance: false. Verify in config.

13.6 401 Unauthorized on VPS warm-up
Cause: API_KEY mismatch between vps_server.py and
config/local.yaml.

Fix: copy exact string from both files side by side. No trailing
spaces. Restart both.

13.7 VPS /stats returns empty
Cause: Binance WS not connecting from VPS region.

Fix:

bash
# From VPS shell
curl "https://api.binance.com/api/v3/ping"
# Should return {}
# If timeout or 451 → region blocked, use Tokyo/Frankfurt/Singapore
13.8 DB fills disk
Symptom: database is locked, or system errors.

Fix:

Reduce retention in vps_server.py:

python
RETENTION_HOURS = 12   # was 24
Or reduce depth levels:

python
bids = sorted(...)[:500]   # was 1000
asks = sorted(...)[:500]
Restart service. Janitor trims on next 5-min cycle.

14. Quick Reference
14.1 Daily operations
bash
# Start bot
python -m app.main

# Start VPS (if separate machine)
ssh root@VPS "systemctl status zephyros-vps"

# Check regime + signal from logs
tail -f logs/app.log | grep -E "Regime|Signal"
14.2 Diagnostic queries
Trades today:

bash
sqlite3 data/trades.db \
  "SELECT COUNT(*), SUM(pnl), MIN(pnl), MAX(pnl) 
   FROM trades 
   WHERE date(entry_ts/1000,'unixepoch') = date('now');"
Open positions:

bash
sqlite3 data/trades.db \
  "SELECT symbol, side, volume, entry_price, entry_ts 
   FROM trades WHERE status = 'open';"
Rejected signals today:

bash
sqlite3 data/trades.db \
  "SELECT symbol, action, reject_reason 
   FROM signals 
   WHERE approved = 0 
     AND date(ts/1000,'unixepoch') = date('now');"
L2 DB row counts:

bash
sqlite3 data/l2_history.db \
  "SELECT symbol, COUNT(*) FROM l2_snapshots GROUP BY symbol;"
14.3 Environment variables
All override config/*.yaml:

bash
BOT_ENV=prod
BOT_LOG_LEVEL=DEBUG
BOT_MT5_LOGIN=416350994
BOT_MT5_PASSWORD=your_password
BOT_MT5_SERVER=Exness-MT5Trial14
14.4 Common file locations
File	Purpose
data/trades.db	All trade + signal history
data/l2_history.db	L2 snapshots + trades (local mode)
data/risk_state.json	Persisted daily risk state
config/local.yaml	Your API keys, mode overrides
config/symbols.local.yaml	Symbol override file
logs/app.log	Runtime logs
.env	Env vars (loaded at startup)
14.5 EMERGENCY
Stop everything immediately:

Close the bot window

Kill any MT5 order by closing position manually in terminal

Close all positions at broker (bypassing bot):

MT5 → right-click position → Close

Bot will reconcile on next start

Rollback to known-good config:

bash
git checkout -- config/default.yaml
git checkout -- app/modules/strategy/absorption_zones.py
# restart bot
15. Changelog
Date	Change
2026-10-22	Regime filter: 5-regime model (Bullish / Bearish / Volatile / Ranging / Neutral) based on L2 CVD+OBI+BVI
2026-10-22	Strategy: disabled mean reversion entirely, momentum-only (void/front breaks)
2026-10-22	Data pipeline: SQLite backend replaces parquet (fixes UI freeze + Binance keepalive)
2026-10-22	Warm-up: replay snapshots + trades from DB, no 15-min cold start
2026-10-22	Exit logic: regime invalidation exit (priority 3)
2026-10-22	VPS recorder: standalone service, own Binance connection, read-only HTTP API
2026-10-22	UI: regime + signal pills in ribbon with hover tooltips
Appendix A: Glossary
Term	Meaning
L2	Level-2 order book — all resting bids/asks with quantities
CVD	Cumulative Volume Delta — Σ(buy) - Σ(sell) aggressive volume
OBI	Order Book Imbalance — bid vs ask depth ratio
BVI	Book Velocity Index — deviation from median book state
HTF	Higher Time Frame — 15m/1h/4h/1d klines
Zone	Reconstructed liquidity band from L2 profiling
Void	Price band with abnormally low resting liquidity
Front edge	Near edge of a zone (the one facing current price)
Far edge	Far edge of a zone (the one away from current price)
Wall	Large concentrated order at a price level
Depletion	Fraction of a wall's original size that has been consumed
Regime	Market classification (Bullish / Bearish / Volatile / Ranging / Neutral)
Magic	MT5 order tag used to distinguish our trades
Appendix B: Failure Mode Matrix
Failure	Bot Behavior	Recovery
Binance WS disconnect	Reconnect w/ exponential backoff	Auto
MT5 disconnect	Reject signals, keep strategy running	Restart MT5, auto-reconnect
VPS unreachable	Warm-up skipped, 15-min live fallback	Restore VPS, restart bot
DB write fail	Buffer dropped, error logged	Fix disk/permissions
Corrupted zone	Zone marked [XX], skipped	Auto (rebuilt next tick)
Config typo	Pydantic validation fails at boot	Fix yaml, restart
Regime stuck WARMING_UP	All signals blocked	Verify samples increasing; else restart
Risk state corrupt	Reset to defaults, warn	Manual review of yesterday
MT5 position vanished	Auto-close in DB with history PnL	Auto
End of file.

If you find anything stale or wrong in this doc, edit it. Code wins
over docs when they disagree — but only if you update the doc to
match the code.

text

---

### Why one file matters

- **`Ctrl+F`** works — search "regime" or "VPS" or "cooldown" and jump straight there
- **Git diffs** show doc updates alongside code updates
- **`cat ZEPHYROS.md`** on the VPS is a full operations manual
- **No documentation debt** — one file to maintain
- **Onboarding** — hand someone this and they can run the bot

It replaces a wiki, three loose `.md` files, and the mental model you build from reading the code. When in doubt, `ZEPHYROS.md` answers.
