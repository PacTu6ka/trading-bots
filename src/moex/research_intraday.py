"""Research low-frequency intraday candidates for the MOEX intraday bot.

Run from the project root:

    .codex_venv/Scripts/python.exe -m src.moex.research_intraday --refresh

The script selects the top N shares by traded value inside the research window,
backtests all configured candidates, and writes CSV/Markdown reports under
data/moex/research/.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from src.manager.config import PROJECT_ROOT
from src.moex.data import get_candles
from src.moex.presets_intraday import INTRADAY_CANDIDATES, INTRADAY_COMMON, INTRADAY_UNIVERSE
from src.moex.strategies import IntradayStrategy


RESULTS_DIR = PROJECT_ROOT / "data" / "moex" / "research"
LEGACY_DATA_DIR = PROJECT_ROOT / "МОЙ ТРЕЙДИНГ БОТ" / "data_cache"

BARS_PER_YEAR = 252 * 17 * 12
COMMISSION_PCT = 0.0005
SLIPPAGE_PCT = 0.0002
POSITION_FRACTION = 0.95


@dataclass
class Metrics:
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    num_trades: int
    trading_days: int
    trades_per_day: float
    win_rate_pct: float
    profit_factor: float
    expectancy_pct: float
    final_equity: float


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research MOEX intraday candidates")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--warmup-months", type=int, default=1)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--max-trades-per-day", type=float, default=1.5)
    parser.add_argument("--min-full-trades", type=int, default=8)
    parser.add_argument("--min-test-trades", type=int, default=1)
    return parser.parse_args()


def _load_candles(ticker: str, months: int, refresh: bool) -> pd.DataFrame:
    df = get_candles(
        ticker=ticker,
        interval="5min",
        months_back=months,
        force_refresh=refresh,
        max_bars=120_000,
        min_bars=500,
    )
    if not df.empty:
        return _normalize_ohlcv(df)

    legacy = LEGACY_DATA_DIR / f"{ticker}_5min.parquet"
    if legacy.exists():
        return _normalize_ohlcv(pd.read_parquet(legacy))

    return pd.DataFrame()


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        for column in ("timestamp", "begin", "datetime", "date"):
            if column in df.columns:
                df = df.set_index(pd.to_datetime(df[column]))
                break

    keep = [c for c in ("open", "high", "low", "close", "volume", "value") if c in df.columns]
    df = df[keep].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df.dropna(subset=["open", "high", "low", "close"])


def _window_start(months: int) -> pd.Timestamp:
    return pd.Timestamp.now().replace(tzinfo=None).normalize() - pd.DateOffset(months=months)


def _slice_window(df: pd.DataFrame, months: int) -> pd.DataFrame:
    if df.empty:
        return df
    end = pd.Timestamp.now().replace(tzinfo=None)
    start = _window_start(months)
    return df[(df.index >= start) & (df.index <= end)].copy()


def _turnover(df: pd.DataFrame) -> float:
    if "value" in df:
        return float(df["value"].fillna(0.0).sum())
    if "volume" in df:
        return float((df["close"] * df["volume"]).fillna(0.0).sum())
    return 0.0


def _select_top_by_turnover(data: dict[str, pd.DataFrame], top_n: int) -> pd.DataFrame:
    rows = []
    for ticker, df in data.items():
        rows.append(
            {
                "ticker": ticker,
                "turnover": _turnover(df),
                "data_start": df.index.min() if not df.empty else pd.NaT,
                "data_end": df.index.max() if not df.empty else pd.NaT,
                "bars": len(df),
                "trading_days": int(df.index.normalize().nunique()) if not df.empty else 0,
            }
        )
    ranked = pd.DataFrame(rows).sort_values("turnover", ascending=False)
    return ranked.head(top_n)


def _backtest(
    df: pd.DataFrame,
    strategy: IntradayStrategy,
    start_at: pd.Timestamp | None = None,
) -> tuple[Metrics, pd.Series, pd.DataFrame]:
    if df.empty or len(df) < 100:
        empty = Metrics(0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0, 1_000_000.0)
        return empty, pd.Series(dtype=float), pd.DataFrame()

    raw_full = strategy.generate_signals(df).reindex(df.index).fillna(0).astype(int)
    if start_at is not None:
        eval_df = df[df.index >= start_at].copy()
        if eval_df.empty:
            empty = Metrics(0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0, 1_000_000.0)
            return empty, pd.Series(dtype=float), pd.DataFrame()
        raw = raw_full.reindex(eval_df.index).fillna(0).astype(int)
        df = eval_df
    else:
        raw = raw_full

    positions = raw.shift(2).fillna(0).astype(int)
    open_ = df["open"].astype(float)
    bar_return = open_.pct_change().fillna(0.0)
    position_change = positions.diff().abs().fillna(positions.abs())
    costs = position_change * (COMMISSION_PCT + SLIPPAGE_PCT)
    net_return = positions * bar_return * POSITION_FRACTION - costs
    equity = 1_000_000.0 * (1 + net_return).cumprod()
    equity.iloc[0] = 1_000_000.0

    trades = _extract_trades(positions, open_)
    metrics = _metrics(equity, trades, int(df.index.normalize().nunique()))
    return metrics, equity, trades


def _extract_trades(positions: pd.Series, prices: pd.Series) -> pd.DataFrame:
    rows = []
    pos = 0
    entry_price = 0.0
    entry_time = None

    for ts, new_pos in positions.items():
        new_pos = int(new_pos)
        if new_pos == pos:
            continue
        if pos != 0 and entry_time is not None:
            exit_price = float(prices.loc[ts])
            pnl_pct = pos * (exit_price / entry_price - 1.0) * POSITION_FRACTION
            pnl_pct -= 2 * (COMMISSION_PCT + SLIPPAGE_PCT)
            rows.append(
                {
                    "entry_time": entry_time,
                    "exit_time": ts,
                    "direction": "long" if pos > 0 else "short",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct * 100,
                }
            )
        if new_pos != 0:
            entry_time = ts
            entry_price = float(prices.loc[ts])
        else:
            entry_time = None
        pos = new_pos

    return pd.DataFrame(rows)


def _metrics(equity: pd.Series, trades: pd.DataFrame, trading_days: int) -> Metrics:
    total_return = (float(equity.iloc[-1]) / float(equity.iloc[0]) - 1.0) * 100
    returns = equity.pct_change().dropna()
    sharpe = 0.0
    if len(returns) > 50 and float(returns.std()) > 0:
        sharpe = float(returns.mean() / returns.std() * np.sqrt(BARS_PER_YEAR))
    drawdown = float(((equity - equity.cummax()) / equity.cummax()).min() * 100)

    if trades.empty:
        return Metrics(total_return, sharpe, drawdown, 0, trading_days, 0.0, 0.0, 0.0, 0.0, float(equity.iloc[-1]))

    wins = trades.loc[trades["pnl_pct"] > 0, "pnl_pct"]
    losses = trades.loc[trades["pnl_pct"] < 0, "pnl_pct"]
    gross_loss = abs(float(losses.sum()))
    profit_factor = float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf")
    return Metrics(
        total_return_pct=total_return,
        sharpe=sharpe,
        max_drawdown_pct=drawdown,
        num_trades=len(trades),
        trading_days=trading_days,
        trades_per_day=len(trades) / max(trading_days, 1),
        win_rate_pct=float((trades["pnl_pct"] > 0).mean() * 100),
        profit_factor=profit_factor,
        expectancy_pct=float(trades["pnl_pct"].mean()),
        final_equity=float(equity.iloc[-1]),
    )


def _score(full: Metrics, test: Metrics, max_trades_per_day: float) -> float:
    trade_penalty = max(0.0, full.trades_per_day - max_trades_per_day) * 5.0
    return (
        full.total_return_pct
        + 2.0 * test.total_return_pct
        + 0.6 * test.sharpe
        + 0.2 * full.sharpe
        + 0.15 * full.max_drawdown_pct
        + 0.10 * test.max_drawdown_pct
        + 0.5 * full.expectancy_pct
        - trade_penalty
    )


def _split_train_test(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    split = df.index[-1] - pd.DateOffset(months=1)
    test_df = df[df.index >= split].copy()
    train_df = df[df.index < split].copy()
    if len(test_df) < 500 or len(train_df) < 1_000:
        split_idx = int(len(df) * 0.8)
        train_df = df.iloc[:split_idx].copy()
        test_df = df.iloc[split_idx:].copy()
    return train_df, test_df


def _metrics_to_row(prefix: str, metrics: Metrics) -> dict[str, float | int]:
    return {
        f"{prefix}_return_pct": metrics.total_return_pct,
        f"{prefix}_sharpe": metrics.sharpe,
        f"{prefix}_max_dd_pct": metrics.max_drawdown_pct,
        f"{prefix}_trades": metrics.num_trades,
        f"{prefix}_trading_days": metrics.trading_days,
        f"{prefix}_trades_per_day": metrics.trades_per_day,
        f"{prefix}_win_rate_pct": metrics.win_rate_pct,
        f"{prefix}_profit_factor": metrics.profit_factor,
        f"{prefix}_expectancy_pct": metrics.expectancy_pct,
    }


def _candidate_params() -> Iterable[tuple[str, dict]]:
    for strategy_name, params in INTRADAY_CANDIDATES.items():
        yield strategy_name, {**INTRADAY_COMMON, **params}


def _format_cell(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _markdown_table(df: pd.DataFrame, columns: list[str] | None = None, limit: int | None = None) -> str:
    if columns is not None:
        df = df[columns]
    if limit is not None:
        df = df.head(limit)
    if df.empty:
        return ""

    headers = list(df.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_format_cell(row[col]) for col in headers) + " |")
    return "\n".join(lines)


def _write_summary(
    path: Path,
    ranked: pd.DataFrame,
    result: pd.DataFrame,
    picks: pd.DataFrame,
    months: int,
    warmup_months: int,
    top_n: int,
    max_trades_per_day: float,
) -> None:
    lines = [
        "# MOEX intraday research",
        "",
        f"Window: last {months} months ending at script run time.",
        f"Signal warmup: {warmup_months} month(s) before the measured window.",
        f"Top universe size: {top_n}, ranked by 5-minute candle turnover value.",
        f"Trade frequency target: <= {max_trades_per_day:.2f} entries per trading day.",
        "",
        "## Selected top shares",
        "",
        _markdown_table(ranked),
        "",
        "## Picks",
        "",
    ]
    if picks.empty:
        lines.append("No candidate passed the robustness and trade-frequency filters.")
    else:
        pick_cols = [
            "ticker",
            "strategy",
            "score",
            "full_return_pct",
            "full_trades_per_day",
            "full_profit_factor",
            "test_return_pct",
            "test_trades_per_day",
            "full_max_dd_pct",
        ]
        lines.append(_markdown_table(picks, pick_cols))

    lines.extend(
        [
            "",
            "## Top rows",
            "",
            _markdown_table(
                result,
                [
                    "ticker",
                    "strategy",
                    "score",
                    "full_return_pct",
                    "full_trades_per_day",
                    "full_profit_factor",
                    "test_return_pct",
                    "test_trades_per_day",
                    "full_max_dd_pct",
                ],
                limit=30,
            ),
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    load_months = args.months + max(args.warmup_months, 0)
    window_start = _window_start(args.months)
    full_data = {
        ticker: _load_candles(ticker, load_months, args.refresh)
        for ticker in INTRADAY_UNIVERSE
    }
    window_data = {ticker: _slice_window(df, args.months) for ticker, df in full_data.items()}
    ranked = _select_top_by_turnover(window_data, args.top_n)
    top_tickers = ranked["ticker"].tolist()
    ranked.to_csv(RESULTS_DIR / "intraday_top10_universe.csv", index=False)

    rows: list[dict] = []
    for ticker in top_tickers:
        df = full_data[ticker]
        eval_df = window_data[ticker]
        if df.empty or eval_df.empty:
            continue

        train_df, test_df = _split_train_test(eval_df)
        train_source = df[df.index <= train_df.index.max()].copy()
        test_source = df[df.index <= test_df.index.max()].copy()
        for strategy_name, strategy_params in _candidate_params():
            strategy = IntradayStrategy(params=strategy_params)
            full, _, _ = _backtest(df, strategy, start_at=window_start)
            train, _, _ = _backtest(train_source, strategy, start_at=window_start)
            test, _, _ = _backtest(test_source, strategy, start_at=test_df.index.min())
            row = {
                "ticker": ticker,
                "strategy": strategy_name,
                "kind": strategy_params["kind"],
                "score": _score(full, test, args.max_trades_per_day),
                "params": repr(strategy_params),
                "turnover": float(ranked.loc[ranked["ticker"] == ticker, "turnover"].iloc[0]),
                "data_start": eval_df.index.min(),
                "data_end": eval_df.index.max(),
                "bars": len(eval_df),
            }
            row.update(_metrics_to_row("full", full))
            row.update(_metrics_to_row("train", train))
            row.update(_metrics_to_row("test", test))
            rows.append(row)

    result = pd.DataFrame(rows)
    if result.empty:
        print("No results.")
        return

    result = result.sort_values("score", ascending=False)
    result.to_csv(RESULTS_DIR / "intraday_top10_research.csv", index=False)

    robust = result[
        (result["full_trades"] >= args.min_full_trades)
        & (result["test_trades"] >= args.min_test_trades)
        & (result["full_trades_per_day"] <= args.max_trades_per_day)
        & (result["full_return_pct"] > 0)
        & (result["test_return_pct"] > 0)
        & (result["full_profit_factor"] >= 1.0)
        & (result["full_max_dd_pct"] > -25)
        & (result["train_return_pct"] > -10)
    ].copy()
    picks = robust.sort_values("score", ascending=False).drop_duplicates("ticker")
    picks.to_csv(RESULTS_DIR / "intraday_top10_picks.csv", index=False)
    _write_summary(
        RESULTS_DIR / "intraday_top10_summary.md",
        ranked,
        result,
        picks,
        args.months,
        args.warmup_months,
        args.top_n,
        args.max_trades_per_day,
    )

    columns = [
        "ticker",
        "strategy",
        "score",
        "full_return_pct",
        "full_trades_per_day",
        "full_profit_factor",
        "test_return_pct",
        "test_trades_per_day",
        "full_max_dd_pct",
    ]
    print("\nTOP SHARES")
    print(ranked.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print("\nTOP 30")
    print(result[columns].head(30).to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print("\nPICKS")
    if picks.empty:
        print("No robust low-frequency picks under the configured filters.")
    else:
        print(picks[columns].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))


if __name__ == "__main__":
    main()
