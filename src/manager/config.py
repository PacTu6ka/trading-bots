"""Configuration loading for the multi-bot server process."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TREND_DAILY_LOSS_LIMIT_RUB = 70_000.0


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    admin_ids: set[str]
    chat_id: str

    @property
    def enabled(self) -> bool:
        return bool(self.token and (self.admin_ids or self.chat_id))

    @property
    def targets(self) -> list[str]:
        out = []
        if self.chat_id:
            out.append(self.chat_id)
        out.extend(sorted(self.admin_ids - set(out)))
        return out


@dataclass(frozen=True)
class BotConfig:
    name: str
    strategy: str
    enabled: bool
    ticker: str
    portfolio: str
    poll_interval: int
    cash_usage: float = 0.99
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.03
    daily_loss_limit_rub: float = DEFAULT_TREND_DAILY_LOSS_LIMIT_RUB
    interval: str = "1h"
    position_rub: float = 0.0
    max_bars: int | None = None
    max_open_positions: int | None = None


@dataclass(frozen=True)
class ManagerConfig:
    arena_token: str
    bots: list[BotConfig]
    telegram: TelegramConfig
    market_data_max_bars: int
    log_level: str
    log_max_bytes: int
    log_backup_count: int

    @property
    def enabled_bots(self) -> list[BotConfig]:
        return [bot for bot in self.bots if bot.enabled]


def load_env_file(path: Path | None = None) -> None:
    """Load .env into os.environ without overriding panel-provided env vars."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_manager_config(log_level_override: str | None = None) -> ManagerConfig:
    load_env_file()

    market_data_max_bars = _env_int("MARKET_DATA_MAX_BARS", 600)
    os.environ["MARKET_DATA_MAX_BARS"] = str(market_data_max_bars)

    bots = _default_bots()
    enabled_override = _csv_env("ENABLED_BOTS")
    if enabled_override:
        enabled_set = {item.lower() for item in enabled_override}
        bots = [
            _replace_enabled(bot, bot.name.lower() in enabled_set or "all" in enabled_set)
            for bot in bots
        ]

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    admin_ids = set(_csv_env("TELEGRAM_ADMIN_IDS"))
    if chat_id:
        admin_ids.add(chat_id)

    return ManagerConfig(
        arena_token=os.environ.get("ARENA_TOKEN", "").strip(),
        bots=bots,
        telegram=TelegramConfig(
            token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_ids=admin_ids,
            chat_id=chat_id,
        ),
        market_data_max_bars=market_data_max_bars,
        log_level=(log_level_override or os.environ.get("LOG_LEVEL", "INFO")).upper(),
        log_max_bytes=_env_int("LOG_MAX_BYTES", 1_048_576),
        log_backup_count=_env_int("LOG_BACKUP_COUNT", 5),
    )


def config_check_lines(config: ManagerConfig) -> list[str]:
    lines = [
        "Config check:",
        f"  ARENA_TOKEN: {'set' if config.arena_token else 'missing'}",
        f"  TELEGRAM_BOT_TOKEN: {'set' if config.telegram.token else 'missing'}",
        f"  Telegram access: {'enabled' if config.telegram.enabled else 'disabled'}",
        f"  MARKET_DATA_MAX_BARS: {config.market_data_max_bars}",
        f"  Enabled bots: {', '.join(bot.name for bot in config.enabled_bots) or 'none'}",
    ]
    for bot in config.bots:
        extras = [
            f"interval={bot.interval}",
            f"max_bars={bot.max_bars or config.market_data_max_bars}",
        ]
        if bot.position_rub:
            extras.append(f"position_rub={bot.position_rub:,.0f}")
        if bot.max_open_positions is not None:
            extras.append(f"max_open={bot.max_open_positions}")
        lines.append(
            f"  - {bot.name}: enabled={bot.enabled}, strategy={bot.strategy}, "
            f"portfolio={bot.portfolio!r}, poll={bot.poll_interval}s, "
            f"{', '.join(extras)}"
        )
    return lines


def missing_required_env(config: ManagerConfig) -> list[str]:
    missing = []
    if not config.arena_token:
        missing.append("ARENA_TOKEN")
    if not config.enabled_bots:
        missing.append("at least one enabled bot")
    return missing


def _default_bots() -> list[BotConfig]:
    return [
        BotConfig(
            name="btc_bollinger",
            strategy="bollinger",
            enabled=_env_bool("BTC_BOLLINGER_ENABLED", True),
            ticker=os.environ.get("BTC_BOLLINGER_TICKER", "BTC").upper(),
            portfolio=os.environ.get("BTC_BOLLINGER_PORTFOLIO", "btc"),
            poll_interval=_env_int("BTC_BOLLINGER_POLL", _env_int("BOT_POLL_INTERVAL", 60)),
            cash_usage=_env_float("BTC_BOLLINGER_CASH_USAGE", 0.99),
            stop_loss_pct=_env_float("BTC_BOLLINGER_STOP_LOSS", 0.02),
            take_profit_pct=_env_float("BTC_BOLLINGER_TAKE_PROFIT", 0.03),
            interval="1h",
        ),
        BotConfig(
            name="btc_trend",
            strategy="trend",
            enabled=_env_bool("BTC_TREND_ENABLED", False),
            ticker=os.environ.get("BTC_TREND_TICKER", "BTC").upper(),
            portfolio=os.environ.get("BTC_TREND_PORTFOLIO", "btc trend"),
            poll_interval=_env_int("BTC_TREND_POLL", _env_int("BOT_POLL_INTERVAL", 60)),
            daily_loss_limit_rub=_env_float(
                "BTC_TREND_DAILY_LOSS_LIMIT_RUB",
                DEFAULT_TREND_DAILY_LOSS_LIMIT_RUB,
            ),
            interval="multi",
        ),
        BotConfig(
            name="moex_portfolio",
            strategy="moex_portfolio",
            enabled=_env_bool("MOEX_PORTFOLIO_ENABLED", False),
            ticker="MOEX_PORTFOLIO",
            portfolio=os.environ.get("MOEX_PORTFOLIO_PORTFOLIO", "saux hak"),
            poll_interval=_env_int("MOEX_PORTFOLIO_POLL", _env_int("BOT_POLL_INTERVAL", 60)),
            stop_loss_pct=_env_float("MOEX_PORTFOLIO_STOP_LOSS", 0.03),
            take_profit_pct=_env_float("MOEX_PORTFOLIO_TAKE_PROFIT", 0.025),
            interval="1h",
            position_rub=_env_float("MOEX_PORTFOLIO_POSITION_RUB", 200_000.0),
            max_bars=_env_optional_int("MOEX_PORTFOLIO_MAX_BARS") or 600,
        ),
        BotConfig(
            name="moex_intraday",
            strategy="moex_intraday",
            enabled=_env_bool("MOEX_INTRADAY_ENABLED", False),
            ticker="MOEX_INTRADAY",
            portfolio=os.environ.get("MOEX_INTRADAY_PORTFOLIO", "saux intraday"),
            poll_interval=_env_int("MOEX_INTRADAY_POLL", _env_int("BOT_POLL_INTERVAL", 60)),
            stop_loss_pct=_env_float("MOEX_INTRADAY_STOP_LOSS", 0.012),
            take_profit_pct=_env_float("MOEX_INTRADAY_TAKE_PROFIT", 0.018),
            interval="5min",
            position_rub=_env_float("MOEX_INTRADAY_POSITION_RUB", 200_000.0),
            max_bars=_env_optional_int("MOEX_INTRADAY_MAX_BARS") or 1200,
            max_open_positions=_env_optional_int("MOEX_INTRADAY_MAX_OPEN_POSITIONS") or 5,
        ),
    ]


def _replace_enabled(bot: BotConfig, enabled: bool) -> BotConfig:
    return replace(bot, enabled=enabled)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 1)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return max(int(raw), 1)
    except ValueError:
        return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]
