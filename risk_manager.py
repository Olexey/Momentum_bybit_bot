"""
Momentum Scalper Bot — Риск Менеджмент
Контроль позиций, размер, SL/TP, кулдауны
"""

import time
import math
from rich.console import Console

import config
from scanner import MarketScanner

console = Console()


class RiskManager:
    """Контроль рисков и управление размером позиций."""

    def __init__(self, scanner, exchange=None):
        self.scanner = scanner
        self.exchange = exchange
        self._cooldowns: dict[str, float] = {}
        self._active_symbols: dict[str, str] = {}

        # ── Daily Drawdown Protection ──
        self._day_start_balance: float = 0.0      # Баланс на начало дня
        self._peak_balance: float = 0.0           # Пиковый баланс (для drawdown)
        self._daily_loss_halt: bool = False        # Стоп торговли на день
        self._drawdown_pause_until: float = 0.0   # Пауза до timestamp
        self._consecutive_losses: int = 0         # Подряд стопов

    def _now(self) -> float:
        """Текущее время (биржевое если доступно)."""
        if self.exchange:
            return self.exchange.now()
        return time.time()

    def set_day_start_balance(self, balance: float):
        """Установить баланс на начало дня / при старте бота."""
        self._day_start_balance = balance
        self._peak_balance = max(self._peak_balance, balance)
        self._daily_loss_halt = False
        self._consecutive_losses = 0

    def update_balance(self, balance: float) -> tuple[bool, str]:
        """
        Проверить баланс на daily drawdown и equity curve protection.
        Возвращает (можно_торговать, причина).
        """
        if balance <= 0:
            return False, "Баланс = 0"

        # Обновляем пик
        self._peak_balance = max(self._peak_balance, balance)

        # ── Daily Loss Check ──
        if self._day_start_balance > 0:
            daily_loss_pct = (self._day_start_balance - balance) / self._day_start_balance
            if daily_loss_pct >= config.MAX_DAILY_LOSS_PCT:
                self._daily_loss_halt = True
                return False, (
                    f"DAILY STOP: -{daily_loss_pct*100:.1f}% "
                    f"(лимит -{config.MAX_DAILY_LOSS_PCT*100:.0f}%)"
                )

        # ── Equity Curve Protection ──
        if self._peak_balance > 0:
            drawdown_pct = (self._peak_balance - balance) / self._peak_balance
            if drawdown_pct >= config.MAX_DRAWDOWN_PCT:
                pause_duration = 3600  # 1 час
                self._drawdown_pause_until = self._now() + pause_duration
                return False, (
                    f"DRAWDOWN PAUSE: -{drawdown_pct*100:.1f}% от пика "
                    f"(лимит -{config.MAX_DRAWDOWN_PCT*100:.0f}%), пауза 1ч"
                )

        # ── Drawdown Pause Active ──
        if self._drawdown_pause_until > 0:
            if self._now() < self._drawdown_pause_until:
                remaining = (self._drawdown_pause_until - self._now()) / 60
                return False, f"Drawdown пауза: {remaining:.0f} мин осталось"
            else:
                self._drawdown_pause_until = 0.0  # Пауза закончилась

        # ── Daily Halt ──
        if self._daily_loss_halt:
            return False, "Daily stop активен (перезапустите бота для сброса)"

        return True, "OK"

    def record_loss(self):
        """Записать потерю (для трекинга серий)."""
        self._consecutive_losses += 1

    def record_win(self):
        """Записать выигрыш (сбрасывает серию)."""
        self._consecutive_losses = 0

    def update_active_positions(self, positions: list):
        """Обновить список активных позиций."""
        self._active_symbols.clear()
        for p in positions:
            symbol = p.get("symbol", "")
            side = p.get("side", "")
            if side == "Buy":
                self._active_symbols[symbol] = "LONG"
            elif side == "Sell":
                self._active_symbols[symbol] = "SHORT"

    def can_open_position(
        self, symbol: str, direction: str, open_count: int
    ) -> tuple[bool, str]:
        """
        Проверить можно ли открыть позицию.
        Возвращает (можно, причина).
        """
        # Макс позиций
        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, f"Макс позиций ({config.MAX_OPEN_POSITIONS})"

        # Дубликат: уже есть позиция по этой паре в эту сторону
        if symbol in self._active_symbols:
            existing = self._active_symbols[symbol]
            if existing == direction:
                return False, f"Уже есть {direction} по {symbol}"

        # Кулдаун после стопа
        if symbol in self._cooldowns:
            elapsed = self._now() - self._cooldowns[symbol]
            if elapsed < config.COOLDOWN_SECONDS:
                remaining = config.COOLDOWN_SECONDS - elapsed
                return False, f"Кулдаун {symbol}: {remaining:.0f}с"

        return True, "OK"

    def add_cooldown(self, symbol: str):
        """Добавить кулдаун для пары (после стопа)."""
        self._cooldowns[symbol] = self._now()

    def calculate_position_size(
        self, balance: float, price: float, symbol: str,
        vol_regime: str = "NORMAL", atr: float = 0.0
    ) -> tuple[float, str]:
        """
        Рассчитать размер позиции.
        vol_regime='HIGH' → половинный размер.
        ATR-нормализация: одинаковый $ риск на все пары.
        Возвращает (qty, qty_string).
        """
        info = self.scanner.get_instrument_info(symbol)
        if not info:
            return 0.0, "0"

        # Выбираем pct в зависимости от волатильности
        if vol_regime == "HIGH":
            pct = config.POSITION_SIZE_HIGH_VOL
        else:
            pct = config.POSITION_SIZE_PCT

        # ATR-нормализация: risk_amount / SL_distance = qty
        # Это выравнивает $ риск по всем парам
        if atr > 0 and price > 0:
            sl_distance = atr * config.SL_ATR_MULTIPLIER
            risk_amount = balance * pct
            qty = risk_amount / sl_distance
        else:
            # Fallback: старый расчёт
            position_value = balance * pct * config.LEVERAGE
            if price <= 0:
                return 0.0, "0"
            qty = position_value / price

        # Округление по qtyStep
        qty_step_str = info.get("qtyStep", "0.001")
        qty_step = float(qty_step_str)
        if qty_step > 0:
            decimals = max(0, len(qty_step_str.rstrip('0').split('.')[-1])) if '.' in qty_step_str else 0
            qty = math.floor(qty / qty_step) * qty_step
            qty = round(qty, decimals)

        # Проверяем мин/макс
        min_qty = info.get("minQty", 0)
        max_qty = info.get("maxQty", float("inf"))

        if qty < min_qty:
            return 0.0, "0"
        if qty > max_qty:
            qty = max_qty

        # Форматируем строку
        if qty_step >= 1:
            qty_str = str(int(qty))
        else:
            decimals = len(qty_step_str.rstrip('0').split('.')[-1]) if '.' in qty_step_str else 0
            qty_str = f"{qty:.{decimals}f}"

        return qty, qty_str

    def calculate_sl_tp(
        self,
        price: float,
        atr: float,
        direction: str,
        symbol: str,
        sl_multiplier: float = None,
        tp_multiplier: float = None,
    ) -> tuple[str, str]:
        """
        Рассчитать SL и TP на основе ATR с ДИНАМИЧЕСКИМИ множителями.
        sl_multiplier/tp_multiplier берутся из Signal (адаптированы к волатильности).
        Если не переданы — используются дефолтные из config.
        """
        info = self.scanner.get_instrument_info(symbol)
        tick_size_str = info.get("tickSize", "0.01") if info else "0.01"
        tick_size = float(tick_size_str)
        decimals = len(tick_size_str.rstrip('0').split('.')[-1]) if '.' in tick_size_str else 0

        # Динамические множители (из Signal или config)
        sl_mult = sl_multiplier if sl_multiplier is not None else config.SL_ATR_MULTIPLIER
        tp_mult = tp_multiplier if tp_multiplier is not None else config.TP_ATR_MULTIPLIER

        sl_distance = atr * sl_mult
        tp_distance = atr * tp_mult

        # Компенсация комиссии round-trip в TP (0.1% × 2 = 0.2%)
        commission_distance = price * config.COMMISSION_PCT * 2
        tp_distance = tp_distance + commission_distance

        if direction == "LONG":
            sl_price = price - sl_distance
            tp_price = price + tp_distance
        else:  # SHORT
            sl_price = price + sl_distance
            tp_price = price - tp_distance

        # Округление по tickSize
        if tick_size > 0:
            sl_price = round(round(sl_price / tick_size) * tick_size, decimals)
            tp_price = round(round(tp_price / tick_size) * tick_size, decimals)

        sl_price = max(sl_price, tick_size)
        tp_price = max(tp_price, tick_size)

        sl_str = f"{sl_price:.{decimals}f}"
        tp_str = f"{tp_price:.{decimals}f}"

        return sl_str, tp_str

    def check_position_timeout(self, position: dict) -> bool:
        """Проверить, не просрочена ли позиция по времени."""
        created_time = float(position.get("createdTime", 0))
        if created_time == 0:
            return False
        elapsed_minutes = (time.time() * 1000 - created_time) / 60000
        return elapsed_minutes > config.MAX_HOLD_MINUTES
