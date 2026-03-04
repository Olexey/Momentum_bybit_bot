"""Диагностика: почему 0 сигналов"""
import config
from exchange import BybitExchange
from strategy import MomentumStrategy, VolRegime
from scanner import MarketScanner

exchange = BybitExchange()
scanner = MarketScanner(exchange)
strategy = MomentumStrategy(exchange)

pairs = scanner.refresh_pairs()
print(f"Пар: {len(pairs)}")

# Проверяем BTC trend
btc_bull = strategy._is_btc_bullish()
print(f"BTC bullish: {btc_bull}")

# Тестируем несколько пар вручную
blocked_counts = {"extreme": 0, "extended": 0, "anti_pump": 0, "weak_score": 0,
                  "trend_15m": 0, "btc_regime": 0, "consecutive": 0,
                  "entry_quality": 0, "signal_ok": 0, "no_data": 0}

for symbol in pairs[:20]:
    try:
        total_score = 0.0
        atr_value = 0.0
        vol_info = None
        candle_info_1m = None
        indicators_1m = None
        indicators_15m = None

        for tf in config.TIMEFRAMES:
            klines = exchange.get_klines(symbol, tf)
            df = strategy._klines_to_df(klines)
            if df.empty:
                continue

            indicators = strategy._calculate_indicators(df)
            if not indicators:
                blocked_counts["no_data"] += 1
                continue

            atr_val = indicators["atr"]
            tf_vol = strategy._detect_volatility_regime(df, indicators["atr_series"])
            tf_candle = strategy._analyze_candle(df, atr_val)
            tf_score = strategy._score_timeframe(indicators, tf_candle, tf_vol)
            weight = config.TIMEFRAME_WEIGHTS.get(tf, 0.1)
            total_score += tf_score * weight

            if tf == config.TIMEFRAMES[0]:
                atr_value = atr_val
                vol_info = tf_vol
                candle_info_1m = tf_candle
                indicators_1m = indicators
            if tf == config.TIMEFRAMES[-1]:
                indicators_15m = indicators

        if atr_value == 0 or vol_info is None:
            continue

        # Check filters one by one
        if vol_info["regime"] == VolRegime.EXTREME:
            blocked_counts["extreme"] += 1
            continue

        if candle_info_1m and candle_info_1m["extended"]:
            if candle_info_1m.get("move_ratio", 0) > 2.5:
                blocked_counts["extended"] += 1
                continue

        if candle_info_1m and atr_value > 0:
            last_body = abs(indicators_1m["close"] - indicators_1m["open"])
            if last_body > config.ANTI_PUMP_ATR_MULT * atr_value:
                blocked_counts["anti_pump"] += 1
                continue

        direction = None
        if total_score >= config.SCORE_LONG_THRESHOLD:
            direction = "LONG"
        elif total_score <= config.SCORE_SHORT_THRESHOLD:
            direction = "SHORT"

        if direction is None:
            blocked_counts["weak_score"] += 1
            print(f"  {symbol:15} score={total_score:+.1f} vol={vol_info['regime']} → WEAK SCORE")
            continue

        # Trend filter
        if indicators_15m is not None:
            ema_tf = indicators_15m.get("ema_trend_fast")
            ema_ts = indicators_15m.get("ema_trend_slow")
            if ema_tf is not None and ema_ts is not None:
                if direction == "LONG" and ema_tf < ema_ts:
                    blocked_counts["trend_15m"] += 1
                    print(f"  {symbol:15} score={total_score:+.1f} {direction} → BLOCKED BY TREND (EMA50<EMA200)")
                    continue
                if direction == "SHORT" and ema_tf > ema_ts:
                    blocked_counts["trend_15m"] += 1
                    print(f"  {symbol:15} score={total_score:+.1f} {direction} → BLOCKED BY TREND (EMA50>EMA200)")
                    continue
            else:
                print(f"  {symbol:15} EMA200 not available (len<205)")

        if direction == "SHORT" and btc_bull:
            blocked_counts["btc_regime"] += 1
            print(f"  {symbol:15} score={total_score:+.1f} SHORT → BLOCKED BY BTC BULL")
            continue

        blocked_counts["signal_ok"] += 1
        print(f"  {symbol:15} score={total_score:+.1f} {direction} → ✅ SIGNAL OK")

    except Exception as e:
        print(f"  {symbol}: ERROR {e}")

print(f"\n=== FILTER STATS ===")
for k, v in blocked_counts.items():
    print(f"  {k:15}: {v}")
