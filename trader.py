"""
Momentum Scalper Bot — Трейдер
Исполнение ордеров, мониторинг позиций, трейлинг стоп
"""

import time
import logging
from dataclasses import dataclass, field
from rich.console import Console

import config
from exchange import BybitExchange
from risk_manager import RiskManager
from strategy import Signal
from persistence import BotPersistence

console = Console()
logger = logging.getLogger("trader")


def _safe_float(value, default: float = 0.0) -> float:
    """Безопасная конвертация в float."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass
class TradeRecord:
    """Запись о сделке."""
    symbol: str
    direction: str
    side: str
    entry_price: float
    qty: str
    sl: str
    tp: str
    open_time: float = field(default_factory=time.time)
    pnl: float = 0.0
    status: str = "OPEN"   # OPEN, CLOSED, STOPPED, TIMEOUT


class Trader:
    """Управление торговлей — открытие, мониторинг, закрытие позиций."""

    def __init__(self, exchange: BybitExchange, risk_manager: RiskManager,
                 db: BotPersistence = None):
        self.exchange = exchange
        self.risk_manager = risk_manager
        self.db = db
        self.trade_history: list[TradeRecord] = []
        self.stats = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
        }
        # Локальный трекинг времени открытия — используем exchange.now() (серверное время)
        self._open_times: dict[str, float] = {}          # symbol → exchange.now() при открытии
        # Trailing stop
        self._best_prices: dict[str, float] = {}         # symbol → лучшая маркет цена
        self._trailing_activated: dict[str, bool] = {}   # symbol → активирован ли трейлинг
        self._entry_atrs: dict[str, float] = {}          # symbol → ATR при входе

        # Восстановление состояния из SQLite
        if self.db:
            self._restore_state()

    def _restore_state(self):
        """Восстановить trailing state и stats из SQLite после перезапуска."""
        try:
            # Восстановить trailing states
            trailing = self.db.get_trailing_states()
            for sym, state in trailing.items():
                self._best_prices[sym] = state["best_price"]
                self._trailing_activated[sym] = state["activated"]
                self._entry_atrs[sym] = state["entry_atr"]
                self._open_times[sym] = state["open_time"]

            # Восстановить stats
            db_stats = self.db.get_session_stats()
            if db_stats["total"] > 0:
                self.stats["total_trades"] = db_stats["total"]
                self.stats["wins"] = db_stats["wins"] or 0
                self.stats["losses"] = db_stats["losses"] or 0
                self.stats["total_pnl"] = db_stats["total_pnl"] or 0.0
                console.print(
                    f"[green]✓ Восстановлено из SQLite: {db_stats['total']} сделок, "
                    f"PnL=${db_stats['total_pnl']:.2f}, "
                    f"{len(trailing)} trailing states[/]"
                )

            # Очистить stale opens
            active = self.get_open_symbols()
            self.db.cleanup_stale_opens(active)

        except Exception as e:
            logger.warning(f"Ошибка восстановления из SQLite: {e}")

    def open_trade(self, signal: Signal, balance: float) -> TradeRecord | None:
        """
        Открыть сделку на основе сигнала.
        Возвращает TradeRecord или None.
        """
        symbol = signal.symbol
        direction = signal.direction

        # Расчёт размера позиции (ATR-нормализация + vol regime)
        qty, qty_str = self.risk_manager.calculate_position_size(
            balance, signal.price, symbol,
            vol_regime=signal.vol_regime, atr=signal.atr
        )
        if qty <= 0:
            console.print(f"[yellow]⚠ {symbol}: qty=0 (balance=${balance:.2f}, price={signal.price})[/]")
            return None

        # Установка плеча
        if not self.exchange.set_leverage(symbol):
            console.print(f"[red]✗ {symbol}: не удалось установить плечо[/]")
            return None

        # Расчёт SL/TP — ДИНАМИЧЕСКИЙ (множители из Signal адаптированы к волатильности)
        sl_str, tp_str = self.risk_manager.calculate_sl_tp(
            signal.price, signal.atr, direction, symbol,
            sl_multiplier=signal.sl_multiplier,
            tp_multiplier=signal.tp_multiplier,
        )

        # Определяем сторону ордера
        side = "Buy" if direction == "LONG" else "Sell"

        console.print(
            f"\n[bold magenta]🚀 ОТКРЫТИЕ {direction}[/]"
            f" | {symbol} | Qty: {qty_str}"
            f" | SL: {sl_str} (×{signal.sl_multiplier:.1f})"
            f" | TP: {tp_str} (×{signal.tp_multiplier:.1f})"
            f" | Score: {signal.score:.1f}"
            f" | EQ: {signal.entry_quality:.0f}"
            f" | Vol: {signal.vol_regime}"
        )

        # Размещаем ордер
        result = self.exchange.place_market_order(
            symbol=symbol,
            side=side,
            qty=qty_str,
            take_profit=tp_str,
            stop_loss=sl_str,
        )

        if not result:
            console.print(f"[red]✗ {symbol}: ордер не исполнен (пустой ответ API)[/]")
            return None

        order_id = result.get("orderId", "???")
        console.print(
            f"[green]✓ Ордер исполнен[/] | ID: {order_id}"
        )

        # Создаём запись
        trade = TradeRecord(
            symbol=symbol,
            direction=direction,
            side=side,
            entry_price=signal.price,
            qty=qty_str,
            sl=sl_str,
            tp=tp_str,
        )
        self.trade_history.append(trade)
        self.stats["total_trades"] += 1

        # Сохраняем время открытия через exchange.now()
        now = self.exchange.now()
        self._open_times[symbol] = now
        # Сохраняем ATR для trailing stop
        self._entry_atrs[symbol] = signal.atr
        self._best_prices[symbol] = signal.price
        self._trailing_activated[symbol] = False

        # Сохраняем в SQLite
        if self.db:
            self.db.save_trade(
                symbol=symbol, direction=direction, side=side,
                entry_price=signal.price, qty=qty_str, sl=sl_str, tp=tp_str,
                open_time=now, vol_regime=signal.vol_regime,
                entry_quality=signal.entry_quality,
            )
            self.db.save_trailing_state(
                symbol=symbol, best_price=signal.price,
                activated=False, entry_atr=signal.atr, open_time=now,
            )

        return trade

    def monitor_positions(self) -> list[dict]:
        """
        Мониторинг открытых позиций.
        Проверяет таймауты и обновляет статистику.
        Возвращает список позиций для дашборда.
        """
        positions = self.exchange.get_positions()
        self.risk_manager.update_active_positions(positions)

        active = []
        for pos in positions:
            symbol = pos.get("symbol", "")
            size = _safe_float(pos.get("size"))
            if size == 0:
                continue

            side = pos.get("side", "")
            entry_price = _safe_float(pos.get("avgPrice"))
            mark_price = _safe_float(pos.get("markPrice"))
            unrealised_pnl = _safe_float(pos.get("unrealisedPnl"))
            leverage = pos.get("leverage", config.LEVERAGE)

            # Форматируем для дашборда
            direction = "LONG" if side == "Buy" else "SHORT"

            # Используем БИРЖЕВОЕ время для расчёта удержания позиции
            open_time = self._open_times.get(symbol)
            now = self.exchange.now()
            if open_time:
                elapsed_min = (now - open_time) / 60
                hold_time = f"{elapsed_min:.1f}m"
            else:
                # Позиция открыта не нами — начинаем трекить с текущего момента
                self._open_times[symbol] = now
                hold_time = "0.0m"
                elapsed_min = 0

            active.append({
                "symbol": symbol,
                "direction": direction,
                "side": side,
                "size": size,
                "entry": entry_price,
                "mark": mark_price,
                "pnl": unrealised_pnl,
                "hold_time": hold_time,
                "leverage": leverage,
            })

            # Проверка таймаута — используем ЛОКАЛЬНОЕ время, не API
            if open_time and elapsed_min > config.MAX_HOLD_MINUTES:
                console.print(
                    f"[yellow]⏰ ТАЙМАУТ {symbol} {direction}"
                    f" ({hold_time}) — закрываем[/]"
                )
                self.exchange.close_position(
                    symbol=symbol,
                    side=side,
                    qty=str(size),
                )
                self._record_close(symbol, unrealised_pnl, "TIMEOUT")
                self._open_times.pop(symbol, None)
                continue

            # ─── Trailing Stop ───
            self._update_trailing_stop(symbol, direction, entry_price, mark_price)

        # Чистим трекинг для закрытых позиций
        active_symbols = {a["symbol"] for a in active}
        for sym in list(self._best_prices.keys()):
            if sym not in active_symbols:
                self._best_prices.pop(sym, None)
                self._trailing_activated.pop(sym, None)
                self._entry_atrs.pop(sym, None)
                self._open_times.pop(sym, None)

        return active

    def _record_close(self, symbol: str, pnl: float, status: str):
        """Записать закрытие сделки."""
        self.stats["total_pnl"] += pnl
        if pnl > 0:
            self.stats["wins"] += 1
            self.risk_manager.record_win()
        else:
            self.stats["losses"] += 1
            self.risk_manager.record_loss()

        # Кулдаун при стопе
        if status == "STOPPED" or pnl < 0:
            self.risk_manager.add_cooldown(symbol)

        # Обновляем запись в истории
        for trade in reversed(self.trade_history):
            if trade.symbol == symbol and trade.status == "OPEN":
                trade.pnl = pnl
                trade.status = status
                break

        # Сохраняем в SQLite
        if self.db:
            self.db.close_trade(symbol, pnl, status)
            self.db.remove_trailing_state(symbol)

    def _update_trailing_stop(
        self, symbol: str, direction: str, entry_price: float, mark_price: float
    ):
        """
        Trailing Stop логика.
        Активируется когда цена прошла TRAILING_STOP_ACTIVATION * ATR в нашу сторону.
        Подтягивает SL на TRAILING_STOP_CALLBACK * ATR от лучшей цены.
        """
        atr = self._entry_atrs.get(symbol)
        if not atr or atr == 0:
            return

        # Обновляем лучшую цену
        best = self._best_prices.get(symbol, entry_price)
        if direction == "LONG":
            best = max(best, mark_price)
        else:
            best = min(best, mark_price)
        self._best_prices[symbol] = best

        # Порог активации
        activation_distance = atr * config.TRAILING_STOP_ACTIVATION

        if direction == "LONG":
            profit_distance = best - entry_price
        else:
            profit_distance = entry_price - best

        if profit_distance < activation_distance:
            return  # Ещё не достигли порога

        # Рассчитываем новый SL
        callback_distance = atr * config.TRAILING_STOP_CALLBACK

        # Получаем tick size для округления
        info = self.risk_manager.scanner.get_instrument_info(symbol)
        tick_size_str = info.get("tickSize", "0.01") if info else "0.01"
        tick_size = float(tick_size_str)
        decimals = len(tick_size_str.rstrip('0').split('.')[-1]) if '.' in tick_size_str else 0

        if direction == "LONG":
            new_sl = best - callback_distance
        else:
            new_sl = best + callback_distance

        # Округление по tick size
        if tick_size > 0:
            new_sl = round(round(new_sl / tick_size) * tick_size, decimals)
        new_sl = max(new_sl, tick_size)

        # SL должен быть в прибыльной зоне и лучше предыдущего
        # Находим текущий SL из trade_history
        current_sl = None
        for trade in reversed(self.trade_history):
            if trade.symbol == symbol and trade.status == "OPEN":
                current_sl = _safe_float(trade.sl)
                break

        if current_sl is None:
            return

        # Проверяем что новый SL лучше (выше для LONG, ниже для SHORT)
        should_update = False
        if direction == "LONG" and new_sl > current_sl:
            should_update = True
        elif direction == "SHORT" and new_sl < current_sl:
            should_update = True

        if not should_update:
            return

        new_sl_str = f"{new_sl:.{decimals}f}"

        # Обновляем SL через API (без TP чтобы дать прибыли расти)
        success = self.exchange.set_trading_stop(
            symbol=symbol,
            stop_loss=new_sl_str,
        )

        if success:
            # Обновляем запись
            for trade in reversed(self.trade_history):
                if trade.symbol == symbol and trade.status == "OPEN":
                    trade.sl = new_sl_str
                    break

            if not self._trailing_activated.get(symbol):
                self._trailing_activated[symbol] = True
                console.print(
                    f"[bold cyan]📈 TRAILING активирован {symbol} {direction}"
                    f" | SL → {new_sl_str}[/]"
                )
            else:
                console.print(
                    f"[cyan]📈 Trailing {symbol}: SL → {new_sl_str}[/]"
                )

            # Сохраняем в SQLite
            if self.db:
                self.db.save_trailing_state(
                    symbol=symbol, best_price=best,
                    activated=self._trailing_activated.get(symbol, False),
                    entry_atr=atr, open_time=self._open_times.get(symbol, 0),
                )

    def check_closed_trades(self, prev_symbols: set) -> list[tuple]:
        """
        Проверить какие позиции закрылись (по TP/SL).
        prev_symbols — символы, которые были открыты ранее.
        Возвращает список (symbol, pnl, status) для логирования.
        """
        current_positions = self.exchange.get_positions()
        current_symbols = {
            p["symbol"] for p in current_positions
            if _safe_float(p.get("size")) > 0
        }

        closed_trades = []

        # Позиции которые исчезли — значит закрылись по TP/SL
        closed = prev_symbols - current_symbols
        for symbol in closed:
            # Получаем реальный PnL из API
            pnl = self.exchange.get_closed_pnl(symbol)
            status = "TP" if pnl >= 0 else "SL"

            # Обновляем статистику (сначала! пока trade.status == "OPEN")
            self._record_close(symbol, pnl, status)
            closed_trades.append((symbol, pnl, status))

        return closed_trades

    def get_open_symbols(self) -> set:
        """Получить множество символов с открытыми позициями."""
        positions = self.exchange.get_positions()
        return {
            p["symbol"] for p in positions
            if _safe_float(p.get("size")) > 0
        }

    def get_stats(self) -> dict:
        """Получить статистику торговли."""
        total = self.stats["wins"] + self.stats["losses"]
        winrate = (self.stats["wins"] / total * 100) if total > 0 else 0
        return {
            **self.stats,
            "winrate": winrate,
        }
