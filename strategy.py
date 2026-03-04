"""
Momentum Scalper Bot — Стратегия v2
Волатильный скальпер с динамическим SL/TP, фильтром extended candles,
анализом фитилей и адаптацией к режиму волатильности.
"""

import logging
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from ta.momentum import RSIIndicator, ROCIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from rich.console import Console

import config
from exchange import BybitExchange

console = Console()
logger = logging.getLogger("strategy")


# ═══════════════════════════════════════════════════════════════
# Режимы волатильности
# ═══════════════════════════════════════════════════════════════
class VolRegime:
    LOW = "LOW"           # Сжатие — готовимся к пробою
    NORMAL = "NORMAL"     # Нормальная торговля
    HIGH = "HIGH"         # Высокая вол — шире стопы, быстрее TP
    EXTREME = "EXTREME"   # Экстремальная — не входим!


class Signal:
    """Сигнал стратегии с динамическими параметрами."""

    def __init__(
        self,
        symbol: str,
        direction: str,       # "LONG" или "SHORT"
        score: float,         # от -100 до +100
        atr: float,           # ATR для базовых расчётов
        price: float,         # текущая цена
        vol_regime: str,      # Режим волатильности
        sl_multiplier: float, # Динамический множитель SL
        tp_multiplier: float, # Динамический множитель TP
        entry_quality: float, # Качество входа 0-100
        details: dict = None,
    ):
        self.symbol = symbol
        self.direction = direction
        self.score = score
        self.atr = atr
        self.price = price
        self.vol_regime = vol_regime
        self.sl_multiplier = sl_multiplier
        self.tp_multiplier = tp_multiplier
        self.entry_quality = entry_quality
        self.details = details or {}

    def __repr__(self):
        return (
            f"Signal({self.symbol} {self.direction} "
            f"score={self.score:.1f} eq={self.entry_quality:.0f} "
            f"vol={self.vol_regime} SL×{self.sl_multiplier:.1f} TP×{self.tp_multiplier:.1f})"
        )


class MomentumStrategy:
    """
    Волатильный скальпер v2.
    - Определяет режим волатильности (LOW/NORMAL/HIGH/EXTREME)
    - Фильтрует extended candles (не входим в конце движения)
    - Анализирует фитили (rejection wicks)
    - Динамически адаптирует SL/TP к текущей волатильности
    - Оценивает качество входа (расстояние от EMA, перерастянутость)
    """

    def __init__(self, exchange: BybitExchange):
        self.exchange = exchange
        # BTC market regime cache
        self._btc_trend_bullish: bool | None = None
        self._btc_trend_time: float = 0

    # ─────────────────────────────────────────────────────────
    # Утилиты
    # ─────────────────────────────────────────────────────────
    def _klines_to_df(self, klines: list) -> pd.DataFrame:
        """Конвертировать klines API в DataFrame."""
        if not klines:
            return pd.DataFrame()

        df = pd.DataFrame(
            klines,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")

        # Bybit возвращает от новых к старым — разворачиваем
        df = df.iloc[::-1].reset_index(drop=True)
        return df

    # ─────────────────────────────────────────────────────────
    # Волатильность
    # ─────────────────────────────────────────────────────────
    def _detect_volatility_regime(self, df: pd.DataFrame, atr: pd.Series) -> dict:
        """
        Определить режим волатильности.
        Использует ATR ratio (текущий ATR / средний ATR) и Bollinger Band Width.
        """
        close = df["close"]

        # ATR ratio: текущий ATR vs средний за 20 свечей
        atr_ma = atr.rolling(window=20).mean()
        atr_ratio = atr.iloc[-1] / atr_ma.iloc[-1] if atr_ma.iloc[-1] > 0 else 1.0

        # Bollinger Band Width (нормализованная)
        bb = BollingerBands(close=close, window=20, window_dev=2)
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1])
        bb_width_pct = bb_width / close.iloc[-1] * 100 if close.iloc[-1] > 0 else 0

        # ATR как % от цены
        atr_pct = atr.iloc[-1] / close.iloc[-1] * 100 if close.iloc[-1] > 0 else 0

        # Определяем режим
        if atr_ratio > 2.5 or atr_pct > 3.0:
            regime = VolRegime.EXTREME
        elif atr_ratio > 1.5 or atr_pct > 1.5:
            regime = VolRegime.HIGH
        elif atr_ratio < 0.6 or bb_width_pct < 0.5:
            regime = VolRegime.LOW
        else:
            regime = VolRegime.NORMAL

        return {
            "regime": regime,
            "atr_ratio": atr_ratio,
            "atr_pct": atr_pct,
            "bb_width_pct": bb_width_pct,
        }

    # ─────────────────────────────────────────────────────────
    # Анализ свечей
    # ─────────────────────────────────────────────────────────
    def _analyze_candle(self, df: pd.DataFrame, atr_val: float) -> dict:
        """
        Анализ последних свечей:
        - Extended candle detection (свеча > 2x ATR = НЕ входим)
        - Wick analysis (длинные фитили = rejection)
        - Candle body ratio (маленькое тело = нерешительность)
        """
        if len(df) < 3:
            return {"extended": False, "wick_rejection": 0, "body_ratio": 0.5}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        candle_range = h - l
        body = abs(c - o)

        if atr_val <= 0 or candle_range <= 0:
            return {"extended": False, "wick_rejection": 0, "body_ratio": 0.5}

        # ── Extended candle: тело свечи > 1.8 × ATR → опасно входить ──
        body_atr_ratio = body / atr_val
        is_extended = body_atr_ratio > 1.8

        # ── Wick Analysis ──
        # Верхний фитиль
        upper_wick = h - max(o, c)
        # Нижний фитиль
        lower_wick = min(o, c) - l

        # Rejection score: -100..+100
        # Большой верхний фитиль = медвежий rejection (отрицательный)
        # Большой нижний фитиль = бычий rejection (положительный)
        wick_rejection = 0
        if candle_range > 0:
            upper_ratio = upper_wick / candle_range
            lower_ratio = lower_wick / candle_range

            # Пин-бар: фитиль > 60% свечи = сильный rejection
            if lower_ratio > 0.6:
                wick_rejection = min(80, lower_ratio * 100)  # Бычий
            elif upper_ratio > 0.6:
                wick_rejection = -min(80, upper_ratio * 100)  # Медвежий
            else:
                wick_rejection = (lower_ratio - upper_ratio) * 50

        # Body ratio
        body_ratio = body / candle_range if candle_range > 0 else 0.5

        # ── Проверяем 2-3 последние свечи на серию (3 бычьих подряд = перерастянуто) ──
        consecutive_bull = 0
        consecutive_bear = 0
        total_move = 0
        for i in range(max(0, len(df) - 4), len(df)):
            row = df.iloc[i]
            if row["close"] > row["open"]:
                consecutive_bull += 1
                consecutive_bear = 0
            elif row["close"] < row["open"]:
                consecutive_bear += 1
                consecutive_bull = 0
            total_move += row["close"] - row["open"]

        # Серия движения > 3 свечей подряд: бот НЕ должен входить в эту сторону
        series_exhaustion = 0
        if consecutive_bull >= 3:
            series_exhaustion = -min(50, consecutive_bull * 15)  # Штраф для LONG
        elif consecutive_bear >= 3:
            series_exhaustion = min(50, consecutive_bear * 15)   # Штраф для SHORT

        # Общее движение за последние свечи vs ATR
        move_ratio = abs(total_move) / atr_val if atr_val > 0 else 0

        return {
            "extended": is_extended,
            "body_atr_ratio": body_atr_ratio,
            "wick_rejection": wick_rejection,
            "body_ratio": body_ratio,
            "consecutive_bull": consecutive_bull,
            "consecutive_bear": consecutive_bear,
            "series_exhaustion": series_exhaustion,
            "move_ratio": move_ratio,
        }

    # ─────────────────────────────────────────────────────────
    # Качество входа
    # ─────────────────────────────────────────────────────────
    def _assess_entry_quality(
        self, df: pd.DataFrame, direction: str, indicators: dict, atr_val: float
    ) -> float:
        """
        Оценка качества точки входа (0-100).
        Хороший вход: цена близко к EMA, свеча не extended, есть rejection wick.
        Плохой вход: цена далеко от EMA, большая свеча, в конце серии.
        """
        quality = 50.0  # Базовый
        close = indicators["close"]

        # ── Расстояние от EMA slow (чем ближе = лучше для входа) ──
        ema_s = indicators["ema_slow"]
        if ema_s > 0 and atr_val > 0:
            ema_distance = abs(close - ema_s) / atr_val
            if ema_distance < 0.5:
                quality += 20     # Очень близко к EMA — отличный вход
            elif ema_distance < 1.0:
                quality += 10     # Нормально
            elif ema_distance > 2.0:
                quality -= 20     # Далеко от EMA — рискованный вход
            elif ema_distance > 3.0:
                quality -= 35     # Слишком далеко — плохой вход

        # ── Bollinger Band позиция ──
        bb = BollingerBands(close=df["close"], window=20, window_dev=2)
        bb_upper = bb.bollinger_hband().iloc[-1]
        bb_lower = bb.bollinger_lband().iloc[-1]
        bb_mid = bb.bollinger_mavg().iloc[-1]

        if direction == "LONG":
            # Хорошо если цена у нижней BB (покупаем дёшево)
            if close <= bb_lower:
                quality += 15
            elif close >= bb_upper:
                quality -= 25     # На вершине BB → плохо для LONG
        else:  # SHORT
            if close >= bb_upper:
                quality += 15
            elif close <= bb_lower:
                quality -= 25     # На дне BB → плохо для SHORT

        # ── RSI экстремумы (не входим на крайних значениях) ──
        rsi = indicators["rsi"]
        if direction == "LONG" and rsi > 80:
            quality -= 20        # RSI слишком высокий для LONG
        elif direction == "SHORT" and rsi < 20:
            quality -= 20        # RSI слишком низкий для SHORT

        return max(0, min(100, quality))

    # ─────────────────────────────────────────────────────────
    # Динамический SL/TP
    # ─────────────────────────────────────────────────────────
    def _calculate_dynamic_sl_tp(
        self, vol_info: dict, candle_info: dict, indicators: dict, direction: str
    ) -> tuple[float, float]:
        """
        Динамический расчёт множителей SL/TP на основе:
        - Режима волатильности
        - Размера свечей
        - Качества wick rejection
        """
        regime = vol_info["regime"]
        atr_ratio = vol_info["atr_ratio"]

        # ── Базовые множители по режиму волатильности ──
        if regime == VolRegime.LOW:
            base_sl = 1.0       # Узкий SL при низкой вол
            base_tp = 1.5       # Узкий TP
        elif regime == VolRegime.NORMAL:
            base_sl = 1.5       # Нормальный SL
            base_tp = 2.5       # Нормальный TP
        elif regime == VolRegime.HIGH:
            base_sl = 2.0       # Широкий SL при высокой вол
            base_tp = 2.0       # TP ближе (быстрее забрать прибыль)
        else:  # EXTREME
            base_sl = 3.0       # Очень широкий
            base_tp = 1.5       # TP ещё ближе

        # ── Адаптация по ATR ratio ──
        # Если ATR выше нормы — расширяем SL, сжимаем TP
        if atr_ratio > 1.0:
            vol_adj = min(atr_ratio, 2.0)
            base_sl *= (0.8 + vol_adj * 0.2)   # SL шире
            base_tp *= (1.2 - vol_adj * 0.1)   # TP ближе
        elif atr_ratio < 0.8:
            base_sl *= 0.8     # SL уже при сжатии
            base_tp *= 1.3     # TP дальше (ждём пробоя)

        # ── Адаптация по wick rejection ──
        wick = candle_info.get("wick_rejection", 0)
        if direction == "LONG" and wick > 30:
            # Бычий rejection wick — SL может быть ещё уже, TP дальше
            base_sl *= 0.9
            base_tp *= 1.1
        elif direction == "SHORT" and wick < -30:
            base_sl *= 0.9
            base_tp *= 1.1

        # ── Адаптация по body/ATR ratio ──
        body_atr = candle_info.get("body_atr_ratio", 0.5)
        if body_atr > 1.2:
            # Большая свеча — SL шире, TP ближе
            base_sl *= 1.2
            base_tp *= 0.8

        # Ограничиваем
        base_sl = max(0.5, min(4.0, base_sl))
        base_tp = max(0.8, min(5.0, base_tp))

        # Минимальный SL для LOW vol — не менее 1.2× ATR (иначе выносит мгновенно)
        if regime == VolRegime.LOW and base_sl < 1.2:
            base_sl = 1.2

        return base_sl, base_tp

    # ─────────────────────────────────────────────────────────
    # Индикаторы
    # ─────────────────────────────────────────────────────────
    def _calculate_indicators(self, df: pd.DataFrame) -> dict:
        """Рассчитать все индикаторы для DataFrame."""
        min_period = max(config.EMA_SLOW, config.ATR_PERIOD, config.VOLUME_MA_PERIOD) + 5
        if len(df) < min_period:
            return {}

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # RSI
        rsi = RSIIndicator(close=close, window=config.RSI_PERIOD).rsi()

        # EMA
        ema_fast = EMAIndicator(close=close, window=config.EMA_FAST).ema_indicator()
        ema_slow = EMAIndicator(close=close, window=config.EMA_SLOW).ema_indicator()

        # ROC
        roc = ROCIndicator(close=close, window=config.ROC_PERIOD).roc()

        # ATR
        atr = AverageTrueRange(
            high=high, low=low, close=close, window=config.ATR_PERIOD
        ).average_true_range()

        # Volume MA
        volume_ma = volume.rolling(window=config.VOLUME_MA_PERIOD).mean()

        # VWAP простой
        typical_price = (high + low + close) / 3
        vwap = (typical_price * volume).rolling(20).sum() / volume.rolling(20).sum()

        # Тренд-фильтр: EMA50 vs EMA200 (for 5m/15m timeframes)
        ema_trend_fast = None
        ema_trend_slow = None
        if len(df) >= config.EMA_TREND_SLOW + 5:
            ema_trend_fast = EMAIndicator(
                close=close, window=config.EMA_TREND_FAST
            ).ema_indicator()
            ema_trend_slow = EMAIndicator(
                close=close, window=config.EMA_TREND_SLOW
            ).ema_indicator()

        current = {
            "rsi": rsi.iloc[-1] if not rsi.empty else 50,
            "rsi_prev": rsi.iloc[-2] if len(rsi) > 1 else 50,
            "rsi_prev2": rsi.iloc[-3] if len(rsi) > 2 else 50,
            "ema_fast": ema_fast.iloc[-1] if not ema_fast.empty else 0,
            "ema_slow": ema_slow.iloc[-1] if not ema_slow.empty else 0,
            "ema_fast_prev": ema_fast.iloc[-2] if len(ema_fast) > 1 else 0,
            "ema_slow_prev": ema_slow.iloc[-2] if len(ema_slow) > 1 else 0,
            "roc": roc.iloc[-1] if not roc.empty else 0,
            "atr": atr.iloc[-1] if not atr.empty else 0,
            "atr_series": atr,
            "volume": volume.iloc[-1] if not volume.empty else 0,
            "volume_ma": volume_ma.iloc[-1] if not volume_ma.empty else 1,
            "vwap": vwap.iloc[-1] if not vwap.empty else 0,
            "close": close.iloc[-1],
            "open": df["open"].iloc[-1],
            "high": high.iloc[-1],
            "low": low.iloc[-1],
            # Trend filter
            "ema_trend_fast": ema_trend_fast.iloc[-1] if ema_trend_fast is not None and not ema_trend_fast.empty else None,
            "ema_trend_slow": ema_trend_slow.iloc[-1] if ema_trend_slow is not None and not ema_trend_slow.empty else None,
        }

        return current

    # ─────────────────────────────────────────────────────────
    # Скоринг с фильтрами
    # ─────────────────────────────────────────────────────────
    def _score_timeframe(
        self, indicators: dict, candle_info: dict, vol_info: dict
    ) -> float:
        """
        Momentum score с учётом волатильности и свечных паттернов.
        Возвращает от -100 до +100.
        """
        if not indicators:
            return 0.0

        score = 0.0

        # ══════ RSI Score (до ±25) ══════
        rsi = indicators["rsi"]
        rsi_prev = indicators["rsi_prev"]
        rsi_prev2 = indicators["rsi_prev2"]

        if rsi < config.RSI_OVERSOLD:
            score += 15
            # RSI разворот вверх — ключевой сигнал
            if rsi > rsi_prev and rsi_prev <= rsi_prev2:
                score += 10  # Разворот из перепроданности
        elif rsi > config.RSI_OVERBOUGHT:
            score -= 15
            if rsi < rsi_prev and rsi_prev >= rsi_prev2:
                score -= 10  # Разворот из перекупленности
        else:
            # Тренд зона
            if rsi > 55:
                score += (rsi - 50) * 0.3
            elif rsi < 45:
                score -= (50 - rsi) * 0.3

        # ══════ EMA Score (до ±25) ══════
        ema_f = indicators["ema_fast"]
        ema_s = indicators["ema_slow"]
        ema_f_prev = indicators["ema_fast_prev"]
        ema_s_prev = indicators["ema_slow_prev"]

        if ema_s > 0:
            ema_spread = (ema_f - ema_s) / ema_s * 100
            ema_spread = max(-2, min(2, ema_spread))
            score += ema_spread * 8  # до ±16

            # Свежее пересечение — сильный сигнал
            if ema_f > ema_s and ema_f_prev <= ema_s_prev:
                score += 9   # Bullish cross
            elif ema_f < ema_s and ema_f_prev >= ema_s_prev:
                score -= 9   # Bearish cross

        # ══════ ROC Score (до ±15) ══════
        roc = indicators["roc"]
        roc_clamped = max(-4, min(4, roc))
        score += roc_clamped * 3.5  # до ±14

        # ══════ Volume Score (до ±15) ══════
        vol = indicators["volume"]
        vol_ma = indicators["volume_ma"]

        if vol_ma > 0:
            vol_ratio = vol / vol_ma
            if vol_ratio > config.VOLUME_SPIKE_MULTIPLIER:
                vol_boost = min(15, (vol_ratio - 1) * 8)
                # Объём усиливает ТЕКУЩЕЕ направление
                if score > 0:
                    score += vol_boost
                elif score < 0:
                    score -= vol_boost

        # ══════ VWAP Score (до ±10) ══════
        vwap = indicators["vwap"]
        close = indicators["close"]
        if vwap > 0:
            if close > vwap:
                score += 5    # Выше VWAP — бычий
            else:
                score -= 5    # Ниже VWAP — медвежий

        # ══════ Wick Rejection Score (до ±15) ══════
        wick = candle_info.get("wick_rejection", 0)
        score += wick * 0.18  # до ±14.4

        # ══════ ШТРАФЫ ══════

        # Штраф за серию свечей (exhaustion)
        exhaustion = candle_info.get("series_exhaustion", 0)
        score += exhaustion * 0.5  # Штрафуем направление серии

        # Штраф за extended candle — ЖЁСТКИЙ
        if candle_info.get("extended", False):
            body_atr = candle_info.get("body_atr_ratio", 0)
            # Чем больше свеча, тем сильнее штраф
            ext_penalty = min(40, body_atr * 15)
            # Штрафуем ТЕКУЩЕЕ направление (не входим в конце движения)
            if indicators["close"] > indicators["open"]:
                score -= ext_penalty    # Длинная зелёная → штраф LONG
            else:
                score += ext_penalty    # Длинная красная → штраф SHORT

        # Ограничиваем
        return max(-100, min(100, score))

    # ─────────────────────────────────────────────────────────
    # Полный анализ пары
    # ─────────────────────────────────────────────────────────
    def analyze_pair(self, symbol: str) -> Signal | None:
        """Полный анализ пары по всем таймфреймам."""
        total_score = 0.0
        atr_value = 0.0
        current_price = 0.0
        details = {}
        vol_info = None
        candle_info_1m = None
        indicators_1m = None
        indicators_15m = None
        df_1m = None

        for tf in config.TIMEFRAMES:
            klines = self.exchange.get_klines(symbol, tf)
            df = self._klines_to_df(klines)

            if df.empty:
                continue

            indicators = self._calculate_indicators(df)
            if not indicators:
                continue

            atr_val = indicators["atr"]

            # Волатильность и свечи для каждого TF
            tf_vol = self._detect_volatility_regime(df, indicators["atr_series"])
            tf_candle = self._analyze_candle(df, atr_val)

            # Скоринг с фильтрами
            tf_score = self._score_timeframe(indicators, tf_candle, tf_vol)
            weight = config.TIMEFRAME_WEIGHTS.get(tf, 0.1)
            total_score += tf_score * weight

            details[f"tf_{tf}"] = {
                "score": round(tf_score, 1),
                "rsi": round(indicators["rsi"], 1),
                "vol": tf_vol["regime"],
                "ext": "!" if tf_candle["extended"] else "",
                "wick": round(tf_candle["wick_rejection"], 0),
            }

            # Основной TF для параметров
            if tf == config.TIMEFRAMES[0]:
                atr_value = atr_val
                current_price = indicators["close"]
                vol_info = tf_vol
                candle_info_1m = tf_candle
                indicators_1m = indicators
                df_1m = df

            # 15m TF для тренд-фильтра
            if tf == config.TIMEFRAMES[-1]:
                indicators_15m = indicators

        if current_price == 0 or atr_value == 0 or vol_info is None:
            return None

        # ══════ ФИЛЬТРЫ ВХОДА ══════

        # 1. НЕ входим при EXTREME волатильности
        if vol_info["regime"] == VolRegime.EXTREME:
            return None

        # 2. НЕ входим если последняя свеча extended И движение > 2.5x ATR
        if candle_info_1m and candle_info_1m["extended"]:
            if candle_info_1m.get("move_ratio", 0) > 2.5:
                return None

        # 2b. ANTI-PUMP: не входим если последняя свеча > 1.5× ATR (ловля экстремума)
        if candle_info_1m and atr_value > 0:
            last_body = abs(indicators_1m["close"] - indicators_1m["open"])
            if last_body > config.ANTI_PUMP_ATR_MULT * atr_value:
                return None

        # 3. Определяем направление
        direction = None
        if total_score >= config.SCORE_LONG_THRESHOLD:
            direction = "LONG"
        elif total_score <= config.SCORE_SHORT_THRESHOLD:
            direction = "SHORT"

        if direction is None:
            return None

        # 3b. TREND FILTER: EMA50 vs EMA200 на 15m TF
        if indicators_15m is not None:
            ema_tf = indicators_15m.get("ema_trend_fast")
            ema_ts = indicators_15m.get("ema_trend_slow")
            if ema_tf is not None and ema_ts is not None:
                if direction == "LONG" and ema_tf < ema_ts:
                    return None  # Не лонг против тренда
                if direction == "SHORT" and ema_tf > ema_ts:
                    return None  # Не шорт против тренда

        # 3c. BTC MARKET REGIME: не шортим если BTC в аптренде
        if direction == "SHORT" and self._is_btc_bullish():
            return None

        # 4. НЕ входим в LONG после серии бычьих свечей (и наоборот)
        if candle_info_1m:
            if direction == "LONG" and candle_info_1m["consecutive_bull"] >= 4:
                return None   # Вход в конце бычьей серии — плохо
            if direction == "SHORT" and candle_info_1m["consecutive_bear"] >= 4:
                return None   # Вход в конце медвежьей серии — плохо

        # 5. Качество входа
        entry_quality = self._assess_entry_quality(
            df_1m, direction, indicators_1m, atr_value
        )

        # Минимальное качество входа (из config)
        if entry_quality < config.MIN_ENTRY_QUALITY:
            return None

        # 6. Динамический SL/TP
        sl_mult, tp_mult = self._calculate_dynamic_sl_tp(
            vol_info, candle_info_1m, indicators_1m, direction
        )

        # Корректируем TP по качеству входа
        if entry_quality > 70:
            tp_mult *= 1.15   # Хороший вход → можем подождать больше
        elif entry_quality < 45:
            tp_mult *= 0.85   # Плохой вход → берём прибыль быстрее
            sl_mult *= 0.9    # И SL уже (меньше риска)

        return Signal(
            symbol=symbol,
            direction=direction,
            score=total_score,
            atr=atr_value,
            price=current_price,
            vol_regime=vol_info["regime"],
            sl_multiplier=sl_mult,
            tp_multiplier=tp_mult,
            entry_quality=entry_quality,
            details=details,
        )

    # ─────────────────────────────────────────────────────────
    # BTC Market Regime
    # ─────────────────────────────────────────────────────────
    def _is_btc_bullish(self) -> bool:
        """Проверить тренд BTC (EMA50 > EMA200 на 15m). Кеш 60сек."""
        import time as _time
        now = _time.time()
        if self._btc_trend_bullish is not None and (now - self._btc_trend_time) < 60:
            return self._btc_trend_bullish

        try:
            klines = self.exchange.get_klines("BTCUSDT", "15", limit=250)
            df = self._klines_to_df(klines)
            if len(df) < config.EMA_TREND_SLOW + 5:
                return True  # Не хватает данных — разрешаем всё

            close = df["close"]
            ema50 = EMAIndicator(close=close, window=50).ema_indicator()
            ema200 = EMAIndicator(close=close, window=200).ema_indicator()

            bullish = ema50.iloc[-1] > ema200.iloc[-1]
            self._btc_trend_bullish = bullish
            self._btc_trend_time = now
            return bullish

        except Exception as e:
            logger.warning(f"BTC trend check failed: {e}")
            return True  # При ошибке — разрешаем всё

    # ─────────────────────────────────────────────────────────
    # Сканирование
    # ─────────────────────────────────────────────────────────
    def scan_all(self, pairs: list[str]) -> list[Signal]:
        """Сканировать все пары параллельно и вернуть отсортированные сигналы."""
        signals = []

        # Параллельный анализ через ThreadPool (8 потоков)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(self._safe_analyze, symbol): symbol
                for symbol in pairs
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    signals.append(result)

        # Сортируем по: entry_quality * abs(score)
        signals.sort(
            key=lambda s: s.entry_quality * abs(s.score),
            reverse=True,
        )
        return signals

    def _safe_analyze(self, symbol: str) -> Signal | None:
        """Безопасный вызов analyze_pair с логированием ошибок."""
        try:
            return self.analyze_pair(symbol)
        except Exception as e:
            logger.warning(f"Ошибка анализа {symbol}: {e}")
            return None
