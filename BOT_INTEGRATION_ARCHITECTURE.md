# Архитектура ботов

Этот файл описывает архитектуру ботов по реализации в коде, а не по README. README в `МОЙ ТРЕЙДИНГ БОТ` может отличаться от фактической логики.

Проверенные источники:

- `src/manager/config.py`, `src/manager/core.py`
- `src/live/runner.py`, `src/live/trend_runner.py`
- `src/strategies/bollinger.py`, `src/strategies/trend_divergence.py`
- `МОЙ ТРЕЙДИНГ БОТ/runner.py`
- `МОЙ ТРЕЙДИНГ БОТ/presets.py`
- `МОЙ ТРЕЙДИНГ БОТ/presets_intraday.py`
- `МОЙ ТРЕЙДИНГ БОТ/intraday_strategies.py`
- `МОЙ ТРЕЙДИНГ БОТ/data.py`, `data_feed.py`, `broker.py`

## `btc_bollinger`

Назначение: BTC mean-reversion бот.

Текущие файлы:

- `main.py`
- `src/live/runner.py`
- `src/strategies/bollinger.py`
- `src/data.py`
- `src/live/broker.py`

Архитектура:

```text
Bybit/OKX 1h candles
  -> src.data.get_candles_async()
  -> BollingerStrategy.generate_signals()
  -> LiveTrader.run_step()
  -> ArenaGoBroker
  -> ArenaGo portfolio: btc
```

На чем основывается:

- тикер: `BTC`;
- таймфрейм: `1h`;
- источник свечей: Bybit, fallback OKX;
- стратегия: Bollinger Bands mean reversion;
- индикаторы: Bollinger Bands, RSI;
- вход long: цена ниже/у нижней Bollinger Band и RSI показывает перепроданность;
- вход short: цена выше/у верхней Bollinger Band и RSI показывает перекупленность;
- выход: возврат сигнала в flat, обратный сигнал, stop loss или take profit.

Состояние и риск:

- хранит `current_position`, `entry_price`, `position_qty`;
- синхронизирует позицию через ArenaGo positions;
- размер позиции считается от доступного cash через `cash_usage`;
- trade events пишутся в `trades/btc_trades_YYYYMMDD.jsonl`;
- история свечей ограничивается через `MARKET_DATA_MAX_BARS`.

## `btc_trend`

Назначение: BTC trend/divergence бот с несколькими таймфреймами.

Текущие файлы:

- `main_trend.py`
- `src/live/trend_runner.py`
- `src/strategies/trend_divergence.py`
- `src/data.py`
- `src/live/broker.py`

Архитектура:

```text
Bybit/OKX candles: 1d + 1h + 5min
  -> src.data.get_candles_async()
  -> TrendDivergenceStrategy.score()
  -> choose best TradeProfile
  -> BtcTrendTrader.run_step()
  -> ArenaGoBroker
  -> ArenaGo portfolio: btc trend
```

На чем основывается:

- тикер: `BTC`;
- портфель: `btc trend`;
- профили:
  - `BIG_D1`: `1d`, крупный сигнал, `cash_usage=0.80`, TP `2.5%`, SL `1.5%`;
  - `MEDIUM_H1`: `1h`, средний сигнал, `cash_usage=0.40`, TP `1.5%`, SL `1%`;
  - `SMALL_M5`: `5min`, малый сигнал, `cash_usage=0.15`, TP `0.75%`, SL `0.4%`;
- стратегия считает направление и score по каждому профилю;
- вход разрешен, если score выше порога профиля;
- при противоположном сигнале текущая позиция закрывается.

Состояние и риск:

- состояние хранится в `state/btc_trend_state.json`;
- активный профиль хранится в `active_exit`;
- есть дневной лимит убытка `daily_loss_limit_rub`;
- при достижении дневного лимита бот закрывает экспозицию и ставит `trading_disabled`;
- trade events пишутся в `trades/btc_trend_trades_YYYYMMDD.jsonl`;
- RAM-чувствительность выше, потому что одновременно используются `1d`, `1h`, `5min`.

## `moex_portfolio`

Назначение: портфельный MOEX-бот на 13 акций.

Текущие файлы:

- `МОЙ ТРЕЙДИНГ БОТ/main.py`
- `МОЙ ТРЕЙДИНГ БОТ/runner.py`
- `МОЙ ТРЕЙДИНГ БОТ/presets.py`
- `МОЙ ТРЕЙДИНГ БОТ/data.py`
- `МОЙ ТРЕЙДИНГ БОТ/data_feed.py`
- `МОЙ ТРЕЙДИНГ БОТ/broker.py`

Архитектура:

```text
MOEX ISS 1h candles
  -> data.get_candles_async()
  -> make_strategy(ticker)
  -> Strategy.generate_signals()
  -> LiveTrader._on_new_bar()
  -> ArenaGoBroker
  -> ArenaGo portfolio: saux hak
```

На чем основывается:

- рынок: MOEX shares;
- таймфрейм: `1h`;
- портфель ArenaGo: `saux hak`;
- размер позиции: около `200_000 RUB`;
- short: разрешен;
- риск: stop loss `3%`, take profit `2.5%`;
- тикеры и стратегии берутся из `TICKER_STRATEGY_MAP`, не из README.

Стратегии:

| Тикер | Стратегия | Основа |
|---|---|---|
| `SBER` | `keltner_20` | EMA(20), ATR(14) x 2.0, RSI 35/65, mean reversion к каналу Keltner. |
| `NLMK` | `zscore_bb_and` | Вход только если Z-Score и Bollinger одновременно дают одно направление. |
| `MTSS` | `zscore` | Отклонение цены от EMA(100), std window 50, entry z=2.5. |
| `SNGSP` | `zscore` | Отклонение от EMA(100), std window 50, entry z=2.0. |
| `NVTK` | `bollinger` | Bollinger mean reversion, bb_length=40, std=3.0, RSI 30/70. |
| `GAZP` | `rsi2_tight` | RSI(2) extreme: входы RSI < 5 или > 95, trend filter EMA(50). |
| `MOEX` | `keltner_30` | EMA(30), ATR(14) x 3.0, RSI 30/70. |
| `ALRS` | `rsi2_default` | RSI(2): входы RSI < 10 или > 90, trend filter EMA(100). |
| `GMKN` | `rsi2_tight` | RSI(2) extreme + EMA(50). |
| `AFLT` | `keltner_30` | EMA(30), ATR(14) x 3.0, RSI 30/70. |
| `LKOH` | `rsi2_tight` | RSI(2) extreme + EMA(50). |
| `CHMF` | `bollinger` | Bollinger mean reversion, bb_length=40, std=3.0, RSI 30/70. |
| `MGNT` | `rsi2_default` | RSI(2) default + EMA(100). |

Состояние и риск:

- `LiveTrader` хранит `_last_signal` по каждому тикеру;
- `_entry_prices` хранит цены входа для TP/SL;
- `sync_positions()` восстанавливает состояние из ArenaGo;
- `ArenaGoBroker` работает внутри в shares и конвертирует shares <-> ArenaGo units;
- `TradeHistoryRecorder` фактически подключен в `runner.py`, хотя README говорит "без сохранения истории";
- текущий live path пишет CSV в `trade_history/saux_hak_trades.csv`.

RAM-особенности:

- 13 тикеров x `1h` DataFrame;
- текущий `data_feed.py` после `pd.concat()` не режет историю по max bars;
- для совместной работы с BTC-ботами историю нужно держать ограниченной tail-окном;
- старый `RealTimeDataFeed.run()` является бесконечным loop.

## `moex_intraday`

Назначение: внутридневной MOEX-бот на 5-минутных свечах.

Текущие файлы:

- `МОЙ ТРЕЙДИНГ БОТ/main_intraday.py`
- `МОЙ ТРЕЙДИНГ БОТ/runner.py`
- `МОЙ ТРЕЙДИНГ БОТ/presets_intraday.py`
- `МОЙ ТРЕЙДИНГ БОТ/intraday_strategies.py`
- `МОЙ ТРЕЙДИНГ БОТ/data.py`
- `МОЙ ТРЕЙДИНГ БОТ/data_feed.py`
- `МОЙ ТРЕЙДИНГ БОТ/broker.py`

Архитектура:

```text
MOEX ISS 1min candles
  -> resample to 5min
  -> make_intraday_strategy(ticker)
  -> IntradayStrategy.generate_signals()
  -> session rules
  -> LiveTrader._on_new_bar()
  -> ArenaGoBroker
  -> ArenaGo portfolio: saux intraday
```

На чем основывается:

- рынок: MOEX shares;
- таймфрейм: `5min`;
- данные берутся как `1min` и ресемплируются в `5min`;
- портфель ArenaGo: `saux intraday`;
- размер позиции: около `200_000 RUB`;
- short: разрешен;
- risk: stop loss `1.2%`, take profit `1.8%`;
- максимум открытых позиций: `5`;
- торговые правила сессии:
  - начало: `06:55`;
  - не открывать новые позиции после `23:20`;
  - принудительно закрыть позиции в `23:25`.

Фактическая live-карта стратегий:

| Тикер | Стратегия | Основа |
|---|---|---|
| `MGNT` | `supertrend` | Supertrend direction, ATR period 7, multiplier 2.0. |
| `NLMK` | `supertrend` | Supertrend direction, ATR period 7, multiplier 2.0. |
| `CHMF` | `supertrend_slow` | Supertrend direction, ATR period 14, multiplier 3.0. |
| `NVTK` | `supertrend_slow` | Supertrend direction, ATR period 14, multiplier 3.0. |
| `GAZP` | `supertrend_slow` | Supertrend direction, ATR period 14, multiplier 3.0. |
| `AFLT` | `bollinger_wide_reversion` | Bollinger reversion, bb_length=40, std=2.5, RSI 35/65. |

Возможные стратегии внутри `IntradayStrategy`:

- `rsi2_reversion`;
- `rsi2_trend`;
- `bollinger_reversion`;
- `bb_vwap_reversion`;
- `vwap_atr_reversion`;
- `vwap_pct_reversion`;
- `zscore_ema_reversion`;
- `zscore_vwap_reversion`;
- `macd_momentum`;
- `ema_cross`;
- `supertrend`;
- `donchian_breakout`;
- `keltner_breakout`;
- `keltner_reversion`;
- `volume_momentum`.

Состояние и риск:

- `LiveTrader` хранит `_last_signal` по каждому intraday-тикеру;
- `_entry_prices` используется для intraday TP/SL;
- `force_flat_at` закрывает позицию независимо от сигнала;
- `no_entry_after` блокирует новые входы и развороты в конце дня;
- текущий live path пишет CSV в `trade_history/saux_intraday_trades.csv`.

RAM-особенности:

- intraday тяжелее portfolio-бота, потому что использует `1min -> 5min`;
- нельзя держать полный месяц `1min` и полный месяц `5min` без обрезки;
- для совместной работы с BTC-ботами нужен лимит вроде `MOEX_INTRADAY_MAX_BARS`;
- polling по нескольким тикерам лучше ограничивать semaphore, а не запускать все запросы без лимита.

## Общие компоненты MOEX-ботов

### `МОЙ ТРЕЙДИНГ БОТ/data.py`

```text
aiomoex.get_market_candles()
  -> pandas DataFrame
  -> optional resample
  -> parquet cache
```

Особенности:

- поддерживает `1min`, `10min`, `1h`, `1d`;
- `5min`, `15min`, `30min` строятся через resample из `1min`;
- кэш лежит в `МОЙ ТРЕЙДИНГ БОТ/data_cache`;
- сейчас нет общего memory cache, как в `src/data.py` для BTC;
- сейчас нет параметра `max_bars`.

### `МОЙ ТРЕЙДИНГ БОТ/broker.py`

```text
internal shares
  -> _shares_to_api()
  -> ArenaGo API quantity
  -> _api_to_shares()
  -> internal Position
```

Особенности:

- внутри runner все позиции считаются в акциях;
- ArenaGo API для разных тикеров может принимать не акции, а лоты;
- `ARENAGO_LOT_SIZES` задает размер лота по тикеру;
- broker проверяет cash delta и предупреждает, если lot size похож на ошибочный.

### `МОЙ ТРЕЙДИНГ БОТ/runner.py`

Главный оркестратор MOEX-ботов:

- `LiveTrader.bootstrap_history()` загружает историю;
- `sync_positions()` синхронизирует реальные позиции;
- `_process_initial_signals()` проверяет сигналы сразу после загрузки истории;
- `_on_new_bar()` обрабатывает новые бары;
- `_check_tp_sl()` закрывает позицию по TP/SL;
- `_execute_signal_change()` закрывает/открывает позиции при смене сигнала;
- `create_algo_trader()` создает `moex_portfolio`;
- `create_intraday_trader()` создает `moex_intraday`.

Важный факт: в `runner.py` есть hardcoded `ARENAGO_TOKEN`; архитектурно это не часть бота, а технический долг.

## Совместная работа с BTC-ботами

```text
run_bot_manager.py
  -> BotManager
     -> btc_bollinger task
     -> btc_trend task
     -> moex_portfolio task
     -> moex_intraday task
  -> TelegramController
```

Для совместной работы архитектурно нужны одинаковые методы у каждого live-бота:

- `prepare()`;
- `run_step()`;
- `status_snapshot()`;
- `broker`.

BTC-боты уже примерно соответствуют этому интерфейсу. MOEX-боты сейчас завязаны на бесконечный `LiveTrader.run()` и `RealTimeDataFeed.run()`, поэтому для manager-архитектуры им нужен тонкий adapter, который вызывает обработку по одному шагу.
