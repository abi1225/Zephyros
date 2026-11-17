from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ─── App ────────────────────────────────────────────────────

class WindowCfg(BaseModel):
    title: str
    width: int
    height: int
    min_width: int
    min_height: int
    start_maximized: bool


class AppCfg(BaseModel):
    name: str
    env: Literal["dev", "staging", "prod"]
    log_level: str
    symbols_override_path: str
    database_path: str
    risk_state_path: str
    dev_mode: bool = True
    test_trade_close_sec: int = 5
    window: WindowCfg


# ─── Data Pipeline ──────────────────────────────────────────

class LocalStoreCfg(BaseModel):
    db_path: str
    retention_hours: int = 24
    flush_interval_sec: int = 5
    janitor_interval_sec: int = 300
    warmup_seconds: int = 300
    snapshot_throttle_ms: int = 1000
    vacuum_on_start: bool = False


class VPSStoreCfg(BaseModel):
    api_url: str = ""
    api_key: str = ""
    warmup_seconds: int = 300


class DataPipelineCfg(BaseModel):
    storage_mode: Literal["local", "vps", "disabled"] = "local"
    local: LocalStoreCfg
    vps: VPSStoreCfg


# ─── Synthetic ──────────────────────────────────────────────

class SyntheticCfg(BaseModel):
    enabled: bool = False
    symbol: str = "XAUUSD"
    base_price: float = 2650.00


# ─── Symbols ────────────────────────────────────────────────

class SymbolCfg(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    display: str | None = None
    enabled: bool = True

    source: Literal["binance", "ctrader"] = "binance"
    binance: str | None = None
    ctrader: str | None = None
    mt5: str | None = None

    price_decimals: int = 2
    qty_decimals: int = 4
    tick_size: float | None = None
    min_volume: float | None = None
    max_volume: float | None = None
    volume_step: float | None = None


# ─── Feeds ──────────────────────────────────────────────────

class ReconnectCfg(BaseModel):
    initial_ms: int
    max_ms: int
    multiplier: float


class BinanceCfg(BaseModel):
    enabled: bool
    rest_base: str
    ws_base: str
    depth_stream_speed_ms: int
    depth_snapshot_limit: int
    reconnect: ReconnectCfg


class KlinesCfg(BaseModel):
    enabled: bool
    interval: str
    history_limit: int
    reconnect: ReconnectCfg


class CTraderCfg(BaseModel):
    enabled: bool = False
    account_id: int = 0
    symbol_id_xauusd: int = 0
    environment: Literal["demo", "live"] = "demo"
    host: str = "demo.ctraderapi.com"
    port: int = 5035
    heartbeat_sec: int = 10
    reconnect_initial_ms: int = 1000
    reconnect_max_ms: int = 30000
    infer_trade_side: bool = True


class FeedsCfg(BaseModel):
    binance: BinanceCfg
    klines: KlinesCfg
    ctrader: CTraderCfg


# ─── Derived data ───────────────────────────────────────────

class BookCfg(BaseModel):
    publish_hz: int
    levels_to_keep: int
    resync_on_gap: bool
    staleness_timeout_ms: int


class TapeCfg(BaseModel):
    buffer_size: int
    publish_hz: int
    large_trade_threshold: dict[str, float]


class TickerCfg(BaseModel):
    cvd_window_sec: int
    imbalance_levels: int


# ─── UI ─────────────────────────────────────────────────────

class LayoutCfg(BaseModel):
    content_margins: int = 12
    content_spacing: int = 12
    panel_padding: int = 16
    panel_radius: int = 8
    panel_header_height: int = 32
    panel_shadow: bool = True


class SidebarItemCfg(BaseModel):
    id: str
    icon: str
    label: str
    order: int = 0


class SidebarFooterItemCfg(BaseModel):
    id: str
    icon: str
    label: str


class SidebarCfg(BaseModel):
    collapsed_width: int = 56
    expanded_width: int = 220
    pinned_by_default: bool = False
    items: list[SidebarItemCfg] = []
    footer_items: list[SidebarFooterItemCfg] = []


class RibbonCfg(BaseModel):
    height: int = 52
    brand: str = "L2 · Bot"
    brand_tooltip: str = "L2 Trading Bot"
    hide_trading_on_pages: list[str] = ["symbols"]


class DrawerCfg(BaseModel):
    collapsed_height: int = 32
    expanded_height: int = 180
    expanded_by_default: bool = False
    title: str = "EVENT STREAM"


class GridCfg(BaseModel):
    cols: int
    rows: int
    gap: int
    padding: int
    row_weights: list[int] | None = None


class FontsCfg(BaseModel):
    ui: str = "Segoe UI"
    mono: str = "Consolas"
    size_sm: int = 11
    size_md: int = 12
    size_lg: int = 13
    size_xl: int = 15
    size_title: int = 18


class SurfacesCfg(BaseModel):
    l0: str = "#09090b"
    l1: str = "#0e0e11"
    l2: str = "#16161a"
    l3: str = "#1d1d22"
    l4: str = "#25252c"


class BordersCfg(BaseModel):
    subtle: str = "rgba(255,255,255,0.06)"
    normal: str = "rgba(255,255,255,0.10)"
    strong: str = "rgba(255,255,255,0.16)"


class TextCfg(BaseModel):
    primary: str = "#e4e4e7"
    secondary: str = "#a1a1aa"
    muted: str = "#71717a"
    disabled: str = "#52525b"


class AccentCfg(BaseModel):
    main: str = "#38bdf8"
    hover: str = "#0ea5e9"
    active: str = "#0284c7"
    subtle: str = "rgba(56,189,248,0.15)"


class SemanticCfg(BaseModel):
    up: str = "#10b981"
    up_hover: str = "#059669"
    up_active: str = "#047857"
    down: str = "#ef4444"
    down_hover: str = "#dc2626"
    down_active: str = "#b91c1c"
    warn: str = "#f59e0b"


class ThemeCfg(BaseModel):
    fonts: FontsCfg = FontsCfg()
    surfaces: SurfacesCfg = SurfacesCfg()
    borders: BordersCfg = BordersCfg()
    text: TextCfg = TextCfg()
    accent: AccentCfg = AccentCfg()
    semantic: SemanticCfg = SemanticCfg()


class PositionCfg(BaseModel):
    row: int
    col: int
    row_span: int
    col_span: int


class WidgetCfg(BaseModel):
    model_config = {"extra": "allow"}
    title: str | None = None
    enabled: bool = True
    position: PositionCfg


class UICfg(BaseModel):
    layout: LayoutCfg = LayoutCfg()
    sidebar: SidebarCfg = SidebarCfg()
    ribbon: RibbonCfg = RibbonCfg()
    drawer: DrawerCfg = DrawerCfg()
    grid: GridCfg
    theme: ThemeCfg = ThemeCfg()
    widgets: dict[str, WidgetCfg]
    candlestick: dict[str, Any] = {}


# ─── Strategy ───────────────────────────────────────────────

class StrategiesCfg(BaseModel):
    model_config = {"extra": "allow"}
    active: str
    subscribe_topic: str = "feed_depth"
    cooldown_ms: int = 500
    min_confidence: float = 0.5
    strategy_timeframe: str = "5m"


# ─── Trade Journal ──────────────────────────────────────────

class TradeJournalCfg(BaseModel):
    enabled: bool = True
    path: str = "data/trade_journal.jsonl"
    flush_interval_sec: float = 5.0
    max_pending: int = 100


# ─── Execution ──────────────────────────────────────────────

class MT5Cfg(BaseModel):
    enabled: bool
    terminal_path: str | None
    login: int | None
    password: str | None
    server: str | None
    magic: int
    deviation_points: int
    filling_mode: Literal["IOC", "FOK", "RETURN"]
    order_timeout_ms: int
    call_timeout_ms: int


class ExecutionCfg(BaseModel):
    broker: str
    mt5: MT5Cfg


# ─── Risk ───────────────────────────────────────────────────

class RiskCfg(BaseModel):
    max_daily_loss_pct: float
    max_position_pct: float
    max_open_positions: int
    max_spread: dict[str, float]
    no_pyramiding: bool = True
    no_martingale: bool = True
    cooldown_after_exit_sec: int = 5
    cooldown_after_loss_sec: int = 60
    max_signal_age_sec: int = 5
    persist_state: bool = True
    daily_reset_hour_utc: int = 0
    publish_interval_sec: int = 1
    allow_manual_close: bool = True
    allow_manual_sl_tp: bool = False

# ─── Protocol ───────────────────────────────────────────────

_TOPIC_DEFAULTS: dict[str, str] = {
    "log_emit": "log_emit",
    "alert": "alert",
    "status_update": "status_update",
    "strategy_state": "strategy_state",
    "position_update": "position_update",
    "feed_depth": "feed_depth",
    "feed_kline": "feed_kline",
    "feed_trade": "feed_trade",
    "mt5_status": "mt5_status",
    "zones_update": "zones_update",
    "signal_proposed": "signal_proposed",
    "signal_approved": "signal_approved",
    "signal_rejected": "signal_rejected",
    "signal_new": "signal_new",
    "signal_failed": "signal_failed",
    "trade_closed": "trade_closed",
    "book_update": "book_update",
    "tape_append": "tape_append",
    "account_update": "account_update",
    "risk_state": "risk_state",
    "debug_decision": "debug.decision",
    "cmd_start_bot": "cmd_start_bot",
    "cmd_stop_bot": "cmd_stop_bot",
    "cmd_log_zones": "cmd_log_zones",
    "cmd_manual_close": "cmd_manual_close",
    "cmd_set_sltp": "cmd_set_sltp",
    "cmd_pause_feed": "cmd_pause_feed",
    "cmd_resume_feed": "cmd_resume_feed",
    "cmd_clear_risk_state": "cmd_clear_risk_state",
    "strategy_changed": "strategy_changed",
    "cmd_select_strategy": "cmd.select_strategy",
    "ui_symbol_changed": "ui_symbol_changed",
    "ui_timeframe_changed": "ui_timeframe_changed",  # Added for Heatmap
}


class ProtocolCfg(BaseModel):
    topics: dict[str, str]

    @model_validator(mode="after")
    def _ensure_all_topics(self):
        for key, default in _TOPIC_DEFAULTS.items():
            self.topics.setdefault(key, default)
        return self

class BotConfig(BaseModel):
    app: AppCfg
    data_pipeline: DataPipelineCfg
    synthetic: SyntheticCfg
    symbols: list[SymbolCfg]
    feeds: FeedsCfg
    book: BookCfg
    tape: TapeCfg
    ticker: TickerCfg
    ui: UICfg
    strategies: StrategiesCfg
    trade_journal: TradeJournalCfg
    execution: ExecutionCfg
    risk: RiskCfg
    protocol: ProtocolCfg


# ─── Env overrides ──────────────────────────────────────────

class _EnvOverrides(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BOT_",
        env_nested_delimiter="__",
        extra="allow",
    )
    env: str | None = None
    log_level: str | None = None
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None

    @field_validator("mt5_login", mode="before")
    @classmethod
    def _empty_to_none_int(cls, v):
        if v is None:
            return None
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("env", "log_level", "mt5_password", "mt5_server", mode="before")
    @classmethod
    def _empty_to_none_str(cls, v):
        if v is None:
            return None
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_symbols(base: list[dict], override: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {s["id"]: dict(s) for s in base}
    order: list[str] = [s["id"] for s in base]
    for s in override:
        sid = s.get("id")
        if not sid:
            continue
        if sid in by_id:
            by_id[sid] = _deep_merge(by_id[sid], s)
        else:
            by_id[sid] = dict(s)
            order.append(sid)
    return [by_id[sid] for sid in order]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if v == "":
            continue
        os.environ.setdefault(k, v)


def load_config(config_dir: str | Path = "config") -> BotConfig:
    config_dir = Path(config_dir)
    _load_dotenv(Path(".env"))

    env = os.getenv("BOT_ENV", "dev")

    merged: dict[str, Any] = _load_yaml(config_dir / "default.yaml")
    merged = _deep_merge(merged, _load_yaml(config_dir / f"{env}.yaml"))
    merged = _deep_merge(merged, _load_yaml(config_dir / "local.yaml"))

    symbols_override = _load_yaml(
        config_dir / "symbols.local.yaml"
    ).get("symbols", [])
    if symbols_override:
        merged["symbols"] = _merge_symbols(
            merged.get("symbols", []), symbols_override,
        )

    envs = _EnvOverrides()
    if envs.env:
        merged.setdefault("app", {})["env"] = envs.env
    if envs.log_level:
        merged.setdefault("app", {})["log_level"] = envs.log_level

    mt5 = merged.setdefault("execution", {}).setdefault("mt5", {})
    if envs.mt5_login is not None:
        mt5["login"] = envs.mt5_login
    if envs.mt5_password is not None:
        mt5["password"] = envs.mt5_password
    if envs.mt5_server is not None:
        mt5["server"] = envs.mt5_server

    return BotConfig.model_validate(merged)


config: BotConfig = load_config()