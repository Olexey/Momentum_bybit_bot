"""
⚡ MOMENTUM SCALPER BOT ⚡
Bybit Demo API v5 | x5 Leverage (Isolated) | Multi-TF Momentum

Скальпер — сканирует все USDT-perp пары,
анализирует momentum на 1m/5m/15m и открывает long/short
с плечом x5 (Isolated Margin). Макс 3 активных позиции.
Daily drawdown protection и equity curve protection.

Запуск:
    python main.py

Перед запуском:
    1. pip install -r requirements.txt
    2. Скопируйте .env.example → .env
    3. Вставьте API ключи от Bybit Demo Trading
"""

import sys
import time
import signal
import logging
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich import box

# Настройка логгирования в файл (ошибки стратегии и других модулей)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("bot_errors.log", encoding="utf-8"),
    ],
)

import config
from exchange import BybitExchange
from scanner import MarketScanner
from strategy import MomentumStrategy
from trader import Trader
from risk_manager import RiskManager
from dashboard import Dashboard
from persistence import BotPersistence

console = Console()


# ═══════════════════════════════════════════════════════════════
# ASCII Banner
# ═══════════════════════════════════════════════════════════════
BANNER = """
[bold magenta]
 ███╗   ███╗ ██████╗ ███╗   ███╗███████╗███╗   ██╗████████╗██╗   ██╗███╗   ███╗
 ████╗ ████║██╔═══██╗████╗ ████║██╔════╝████╗  ██║╚══██╔══╝██║   ██║████╗ ████║
 ██╔████╔██║██║   ██║██╔████╔██║█████╗  ██╔██╗ ██║   ██║   ██║   ██║██╔████╔██║
 ██║╚██╔╝██║██║   ██║██║╚██╔╝██║██╔══╝  ██║╚██╗██║   ██║   ██║   ██║██║╚██╔╝██║
 ██║ ╚═╝ ██║╚██████╔╝██║ ╚═╝ ██║███████╗██║ ╚████║   ██║   ╚██████╔╝██║ ╚═╝ ██║
 ╚═╝     ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝
[/]
[bold cyan]                    ⚡ SCALPER BOT ⚡[/]
[dim]           Bybit Demo API v5 │ x5 Isolated │ Multi-TF[/]
"""


class MomentumScalper:
    """Главный класс бота — оркестрация всех модулей."""

    def __init__(self):
        self.running = False
        self.exchange: BybitExchange | None = None
        self.scanner: MarketScanner | None = None
        self.strategy: MomentumStrategy | None = None
        self.risk_manager: RiskManager | None = None
        self.trader: Trader | None = None
        self.dashboard: Dashboard | None = None
        self.db: BotPersistence | None = None

    def initialize(self) -> bool:
        """Инициализация всех модулей."""
        console.print(BANNER)

        # Проверка API ключей
        if not config.API_KEY or not config.API_SECRET:
            console.print(
                Panel(
                    "[bold red]ОШИБКА: API ключи не заданы![/]\n\n"
                    "1. Скопируйте [cyan].env.example[/] → [cyan].env[/]\n"
                    "2. Вставьте API ключи от Bybit Demo Trading\n"
                    "3. Ключи создаются на bybit.com → API → Demo Trading",
                    title="⚠ Конфигурация",
                    box=box.HEAVY,
                )
            )
            return False

        try:
            # Инициализация модулей
            console.print("\n[bold]Инициализация модулей...[/]\n")

            self.db = BotPersistence()
            self.exchange = BybitExchange()
            self.scanner = MarketScanner(self.exchange)
            self.strategy = MomentumStrategy(self.exchange)
            self.risk_manager = RiskManager(self.scanner, self.exchange)
            self.trader = Trader(self.exchange, self.risk_manager, self.db)
            self.dashboard = Dashboard()

            # Проверяем подключение — получаем баланс
            balance = self.exchange.get_balance()
            console.print(f"[green]✓ Баланс: [bold]${balance:,.2f} USDT[/][/]")

            if balance <= 0:
                console.print(
                    "[yellow]⚠ Баланс = 0. Убедитесь что Demo аккаунт активирован[/]"
                )

            # Первичное сканирование
            pairs = self.scanner.refresh_pairs(force=True)
            if not pairs:
                console.print("[red]✗ Не найдено подходящих пар[/]")
                return False

            console.print(
                f"\n[bold green]✓ Бот готов к работе![/]"
                f"\n[dim]  Пары: {len(pairs)} | Плечо: x{config.LEVERAGE}"
                f" | Макс позиций: {config.MAX_OPEN_POSITIONS}"
                f" | TF: {', '.join(config.TIMEFRAMES)}[/]\n"
            )

            # Сохраняем начальный баланс для дашборда и drawdown protection
            self.dashboard.set_initial_balance(balance)
            self.risk_manager.set_day_start_balance(balance)

            return True

        except Exception as e:
            console.print(f"[red]✗ Ошибка инициализации: {e}[/]")
            return False

    def run(self):
        """Главный цикл бота."""
        self.running = True

        # Инициализируем prev_open_symbols РЕАЛЬНЫМИ позициями с биржи
        prev_open_symbols: set = self.trader.get_open_symbols()

        console.print("[bold yellow]🚀 Бот запущен! Ctrl+C для остановки[/]\n")
        time.sleep(2)

        while self.running:
            try:
                cycle_start = time.time()

                # ─── 1. Обновить список пар ───
                pairs = self.scanner.refresh_pairs()

                # ─── 2. Проверить закрытые по TP/SL (СНАЧАЛА, до мониторинга) ───
                closed_trades = self.trader.check_closed_trades(prev_open_symbols)
                for sym, pnl, status in closed_trades:
                    emoji = "💰" if pnl >= 0 else "📉"
                    color = "green" if pnl >= 0 else "red"
                    self.dashboard.log(
                        f"[bold {color}]{emoji} {sym} закрыт по {status}: "
                        f"PnL = ${pnl:+.4f}[/]"
                    )

                # ─── 3. Мониторинг текущих позиций ───
                active_positions = self.trader.monitor_positions()
                open_count = len(active_positions)

                # Обновляем prev_open_symbols из текущих активных позиций
                prev_open_symbols = {p["symbol"] for p in active_positions}

                # ─── 4. Проверка drawdown protection ───
                balance = self.exchange.get_balance()
                can_trade, dd_reason = self.risk_manager.update_balance(balance)

                # ─── 5. Анализ и торговля (если есть слоты и drawdown OK) ───
                signals = []
                if not can_trade:
                    self.dashboard.log(
                        f"[bold red]⛔ {dd_reason}[/]"
                    )
                    self.dashboard.increment_scan()
                elif open_count < config.MAX_OPEN_POSITIONS:
                    self.dashboard.log(
                        f"[cyan]Сканирование {len(pairs)} пар...[/]"
                    )

                    signals = self.strategy.scan_all(pairs)
                    self.dashboard.set_signals(signals)
                    self.dashboard.increment_scan()

                    if signals:
                        self.dashboard.log(
                            f"[green]Найдено {len(signals)} сигналов. "
                            f"Лучший: {signals[0]}[/]"
                        )

                    # Открываем сделки по лучшим сигналам
                    # Считаем direction exposure
                    long_count = sum(
                        1 for p in active_positions
                        if p.get("side", "") == "Buy"
                    )
                    short_count = sum(
                        1 for p in active_positions
                        if p.get("side", "") == "Sell"
                    )

                    for sig in signals:
                        if open_count >= config.MAX_OPEN_POSITIONS:
                            break

                        # Direction limit (anti-correlation)
                        if sig.direction == "LONG" and long_count >= config.MAX_SAME_DIRECTION:
                            self.dashboard.log(
                                f"[yellow]⚠ {sig.symbol}: макс LONG ({config.MAX_SAME_DIRECTION})[/]"
                            )
                            continue
                        if sig.direction == "SHORT" and short_count >= config.MAX_SAME_DIRECTION:
                            self.dashboard.log(
                                f"[yellow]⚠ {sig.symbol}: макс SHORT ({config.MAX_SAME_DIRECTION})[/]"
                            )
                            continue

                        # Проверка риск-менеджера
                        can_open, reason = self.risk_manager.can_open_position(
                            sig.symbol, sig.direction, open_count
                        )
                        if not can_open:
                            self.dashboard.log(
                                f"[yellow]⚠ {sig.symbol} {sig.direction}: {reason}[/]"
                            )
                            continue

                        # Открываем сделку
                        trade = self.trader.open_trade(sig, balance)
                        if trade:
                            open_count += 1
                            self.dashboard.log(
                                f"[bold green]🚀 ОТКРЫТ {trade.direction} "
                                f"{trade.symbol} qty={trade.qty} "
                                f"SL={trade.sl} TP={trade.tp} "
                                f"Vol={sig.vol_regime} EQ={sig.entry_quality:.0f}[/]"
                            )
                            balance = self.exchange.get_balance()
                        else:
                            self.dashboard.log(
                                f"[red]✗ Не удалось открыть "
                                f"{sig.direction} {sig.symbol} "
                                f"(score={sig.score:.1f})[/]"
                            )
                else:
                    self.dashboard.log(
                        f"[yellow]Макс позиций ({config.MAX_OPEN_POSITIONS}) "
                        f"— ждём закрытия...[/]"
                    )
                    self.dashboard.increment_scan()

                # ─── 6. Отрисовка дашборда (используем кешированный balance) ───
                stats = self.trader.get_stats()
                self.dashboard.render(
                    balance=balance,
                    positions=active_positions,
                    stats=stats,
                )

                # ─── 6. Пауза до следующего цикла ───
                cycle_time = time.time() - cycle_start
                sleep_time = max(0, config.SCAN_INTERVAL_SECONDS - cycle_time)
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except KeyboardInterrupt:
                self.stop()
            except Exception as e:
                console.print(f"[red]✗ Ошибка в цикле: {e}[/]")
                self.dashboard.log(f"[red]ОШИБКА: {e}[/]")
                time.sleep(5)

    def stop(self):
        """Graceful shutdown."""
        self.running = False
        # Закрыть SQLite
        if self.db:
            self.db.close()
        console.print("\n")
        console.print(
            Panel(
                "[bold yellow]⏹ Бот остановлен[/]\n\n"
                + self._final_stats(),
                title="Завершение",
                box=box.DOUBLE,
            )
        )

    def _final_stats(self) -> str:
        """Финальная статистика."""
        if not self.trader:
            return ""
        stats = self.trader.get_stats()
        return (
            f"📊 Всего сделок: {stats['total_trades']}\n"
            f"✅ Wins: {stats['wins']} | ❌ Losses: {stats['losses']}\n"
            f"📈 Win Rate: {stats['winrate']:.1f}%\n"
            f"💵 Total PnL: ${stats['total_pnl']:,.4f}"
        )


def main():
    """Точка входа."""
    bot = MomentumScalper()

    # Обработка Ctrl+C
    def signal_handler(sig, frame):
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Инициализация
    if not bot.initialize():
        console.print("[red]Бот не смог запуститься. Проверьте настройки.[/]")
        sys.exit(1)

    # Запуск
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        console.print(f"[red]✗ Критическая ошибка: {e}[/]")
        import traceback
        traceback.print_exc()
        bot.stop()


if __name__ == "__main__":
    main()
