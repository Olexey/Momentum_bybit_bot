"""
Momentum Scalper Bot — Конфигурация
Bybit Demo API v5 | x40 Leverage
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# API Bybit Demo
# ═══════════════════════════════════════════════════════════════
API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
DEMO_MODE = True      # Demo trading (НЕ testnet!)
TESTNET = False        # Всегда False — используем Demo API

# ═══════════════════════════════════════════════════════════════
# Торговые параметры
# ═══════════════════════════════════════════════════════════════
LEVERAGE = 5                      # Плечо x5 (↓с x40 — безопасный уровень)
MARGIN_MODE = "ISOLATED"           # Isolated margin (изоляция рисков)
CATEGORY = "linear"               # USDT Perpetual
QUOTE_CURRENCY = "USDT"

# ═══════════════════════════════════════════════════════════════
# Параметры стратегии (Momentum Scoring)
# ═══════════════════════════════════════════════════════════════
TIMEFRAMES = ["1", "5", "15"]     # 1m, 5m, 15m
TIMEFRAME_WEIGHTS = {
    "1": 0.50,                    # 1m — основной вес
    "5": 0.30,                    # 5m — подтверждение
    "15": 0.20,                   # 15m — общий тренд
}
KLINE_LIMIT = 250                 # Свечей для анализа (нужно для EMA200)

# Индикаторы
RSI_PERIOD = 7                    # Быстрый RSI
RSI_OVERBOUGHT = 75              # Перекупленность
RSI_OVERSOLD = 25                 # Перепроданность
EMA_FAST = 9                     # Быстрая EMA
EMA_SLOW = 21                    # Медленная EMA
VOLUME_MA_PERIOD = 20            # SMA объёма
VOLUME_SPIKE_MULTIPLIER = 1.3    # Всплеск объёма (x от среднего)
ROC_PERIOD = 5                   # Rate of Change период
ATR_PERIOD = 14                  # ATR для SL/TP

# Пороги для сигналов (асимметричные — SHORT строже)
SCORE_LONG_THRESHOLD = 30         # LONG ≥ 30
SCORE_SHORT_THRESHOLD = -38       # SHORT ≤ -38 (строже — шорт сложнее)

# Тренд-фильтр (EMA50 vs EMA200)
EMA_TREND_FAST = 50               # EMA50 для определения тренда
EMA_TREND_SLOW = 200              # EMA200 — LONG только если EMA50 > EMA200

# ═══════════════════════════════════════════════════════════════
# Риск Менеджмент
# ═══════════════════════════════════════════════════════════════
MAX_OPEN_POSITIONS = 3            # Макс одновременных сделок
MAX_SAME_DIRECTION = 2            # Макс в одну сторону (anti-correlation)
POSITION_SIZE_PCT = 0.01          # 1% баланса на сделку
POSITION_SIZE_HIGH_VOL = 0.005    # 0.5% для HIGH vol (половинный)
SL_ATR_MULTIPLIER = 2.5          # SL = 2.5 × ATR
TP_ATR_MULTIPLIER = 4.0          # TP = 4.0 × ATR
COOLDOWN_SECONDS = 60            # Кулдаун после стопа (сек)
MAX_HOLD_MINUTES = 45            # Макс время удержания сделки (мин)
TRAILING_STOP_ACTIVATION = 0.4   # Активация трейлинга (↓с 0.8)
TRAILING_STOP_CALLBACK = 0.25    # Шаг трейлинга (↓с 0.3)
MIN_ENTRY_QUALITY = 45           # Минимальный Entry Quality
MAX_DAILY_LOSS_PCT = 0.05        # Макс потеря за день: 5% → стоп
MAX_DRAWDOWN_PCT = 0.10          # Макс просадка от пика: 10% → пауза
COMMISSION_PCT = 0.001           # Комиссия тейкер 0.1%
ANTI_PUMP_ATR_MULT = 1.5         # Не входить если свеча > 1.5× ATR

# ═══════════════════════════════════════════════════════════════
# Сканер
# ═══════════════════════════════════════════════════════════════
MIN_24H_VOLUME_USDT = 5_000_000  # Мин 24h объём для торговли
SCANNER_REFRESH_INTERVAL = 300   # Обновление списка пар (сек)
TOP_PAIRS_LIMIT = 40             # Сканировать топ N пар по объёму

# ═══════════════════════════════════════════════════════════════
# Цикл бота
# ═══════════════════════════════════════════════════════════════
SCAN_INTERVAL_SECONDS = 10       # Интервал сканирования (сек)
DASHBOARD_REFRESH = 3            # Обновление дашборда (сек)
