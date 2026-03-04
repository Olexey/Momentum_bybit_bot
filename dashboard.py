"""
Momentum Scalper Bot — Консольный дашборд
Красивый вывод состояния бота через rich
"""

import os
import re
import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.live import Live
from rich import box

import config

console = Console()

LOG_FILE = "bot_log.txt"


class Dashboard:
    """Консольный дашборд для мониторинга бота."""

    def __init__(self):
        self.start_time = time.time()
        self._last_signals: list = []
        self._log_lines: list = []
        self._scan_count = 0
        self._initial_balance: float = 0.0

        # Очищаем лог-файл при старте
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"=== Momentum Scalper Bot — Запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    def set_initial_balance(self, balance: float):
        """Установить начальный баланс для расчёта реального PnL."""
        self._initial_balance = balance

    def log(self, message: str):
        """Добавить запись в лог."""
        timestamp = time.strftime("%H:%M:%S")
        self._log_lines.append(f"[dim]{timestamp}[/] {message}")
        # Храним последние 100 записей в памяти
        if len(self._log_lines) > 100:
            self._log_lines = self._log_lines[-100:]

        # Пишем в файл (чистый текст без rich-разметки)
        clean_msg = re.sub(r'\[.*?\]', '', message)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{timestamp}  {clean_msg}\n")
        except Exception:
            pass

    def set_signals(self, signals: list):
        """Обновить список сигналов."""
        self._last_signals = signals[:10]  # Топ 10

    def increment_scan(self):
        """Увеличить счётчик сканирований."""
        self._scan_count += 1

    def render(
        self,
        balance: float,
        positions: list[dict],
        stats: dict,
    ) -> Panel:
        """Отрисовать полный дашборд."""
        # Очистка консоли
        os.system("cls" if os.name == "nt" else "clear")

        # ═══ ЗАГОЛОВОК ═══
        uptime = time.time() - self.start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)

        header = Text()
        header.append("⚡ MOMENTUM SCALPER ", style="bold magenta")
        header.append("| Bybit Demo ", style="cyan")
        header.append(f"| x{config.LEVERAGE} ", style="bold yellow")
        header.append(f"| Uptime: {hours:02d}:{minutes:02d}:{seconds:02d} ", style="dim")
        header.append(f"| Scans: {self._scan_count}", style="dim")
        console.print(Panel(header, box=box.DOUBLE))

        # ═══ БАЛАНС И СТАТИСТИКА ═══
        stats_table = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
        stats_table.add_column("Key", style="cyan", width=15)
        stats_table.add_column("Value", style="bold white", width=15)
        stats_table.add_column("Key2", style="cyan", width=15)
        stats_table.add_column("Value2", style="bold white", width=15)

        # Реальный PnL из разницы баланса
        real_pnl = balance - self._initial_balance if self._initial_balance > 0 else stats.get("total_pnl", 0)
        pnl_style = "green" if real_pnl >= 0 else "red"
        winrate = stats.get("winrate", 0)

        stats_table.add_row(
            "💰 Баланс",
            f"${balance:,.2f}",
            "📊 Сделок",
            str(stats.get("total_trades", 0)),
        )
        stats_table.add_row(
            "📈 Win Rate",
            f"{winrate:.1f}%",
            "💵 PnL",
            Text(f"${real_pnl:,.4f}", style=pnl_style),
        )
        stats_table.add_row(
            "✅ Wins",
            str(stats.get("wins", 0)),
            "❌ Losses",
            str(stats.get("losses", 0)),
        )

        console.print(Panel(stats_table, title="[bold]Статистика[/]", box=box.ROUNDED))

        # ═══ ОТКРЫТЫЕ ПОЗИЦИИ ═══
        pos_table = Table(
            box=box.ROUNDED,
            title=f"[bold]Открытые позиции ({len(positions)}/{config.MAX_OPEN_POSITIONS})[/]",
            show_lines=True,
        )
        pos_table.add_column("#", style="dim", width=3)
        pos_table.add_column("Пара", style="bold white", width=14)
        pos_table.add_column("Напр.", width=7)
        pos_table.add_column("Вход", style="cyan", width=12)
        pos_table.add_column("Маркет", style="white", width=12)
        pos_table.add_column("PnL", width=12)
        pos_table.add_column("Время", style="dim", width=8)
        pos_table.add_column("Плечо", style="yellow", width=6)

        if positions:
            for i, p in enumerate(positions, 1):
                direction = p["direction"]
                dir_style = "green" if direction == "LONG" else "red"
                dir_emoji = "🟢" if direction == "LONG" else "🔴"

                pnl = p["pnl"]
                pnl_style = "bold green" if pnl >= 0 else "bold red"
                pnl_emoji = "+" if pnl >= 0 else ""

                pos_table.add_row(
                    str(i),
                    p["symbol"],
                    Text(f"{dir_emoji} {direction}", style=dir_style),
                    f"{p['entry']:.4f}" if p['entry'] < 1 else f"{p['entry']:,.2f}",
                    f"{p['mark']:.4f}" if p['mark'] < 1 else f"{p['mark']:,.2f}",
                    Text(f"{pnl_emoji}{pnl:,.4f}", style=pnl_style),
                    p["hold_time"],
                    f"x{p['leverage']}",
                )
        else:
            pos_table.add_row(
                "-", "[dim]Нет открытых позиций[/]",
                "", "", "", "", "", "",
            )

        console.print(pos_table)

        # ═══ СИГНАЛЫ ═══
        if self._last_signals:
            sig_table = Table(
                box=box.SIMPLE,
                title="[bold]Последние сигналы[/]",
            )
            sig_table.add_column("Пара", style="white", width=14)
            sig_table.add_column("Напр.", width=7)
            sig_table.add_column("Score", width=7)
            sig_table.add_column("EQ", width=5)
            sig_table.add_column("Vol", width=8)
            sig_table.add_column("SL/TP", style="dim", width=12)
            sig_table.add_column("Детали", style="dim", width=35)

            for sig in self._last_signals[:5]:
                direction = sig.direction
                dir_style = "green" if direction == "LONG" else "red"
                score_style = "bold green" if sig.score > 0 else "bold red"

                # Цвет для Entry Quality
                eq = sig.entry_quality
                eq_style = "green" if eq >= 60 else "yellow" if eq >= 40 else "red"

                # Цвет для Vol regime
                vol_colors = {"LOW": "blue", "NORMAL": "green", "HIGH": "yellow", "EXTREME": "red"}
                vol_style = vol_colors.get(sig.vol_regime, "white")

                details_parts = []
                for tf_key, tf_data in sig.details.items():
                    tf = tf_key.replace("tf_", "")
                    ext_mark = tf_data.get('ext', '')
                    vol_mark = tf_data.get('vol', '')[0] if tf_data.get('vol') else ''
                    details_parts.append(
                        f"{tf}m:s={tf_data['score']:.0f} r={tf_data['rsi']:.0f}{ext_mark}"
                    )
                details_str = " | ".join(details_parts)

                sig_table.add_row(
                    sig.symbol,
                    Text(direction, style=dir_style),
                    Text(f"{sig.score:.1f}", style=score_style),
                    Text(f"{eq:.0f}", style=eq_style),
                    Text(sig.vol_regime, style=vol_style),
                    f"×{sig.sl_multiplier:.1f}/×{sig.tp_multiplier:.1f}",
                    details_str,
                )

            console.print(sig_table)

        # ═══ ЛОГ ═══
        if self._log_lines:
            log_text = "\n".join(self._log_lines[-15:])
            console.print(
                Panel(
                    log_text,
                    title=f"[bold]Лог[/] [dim](полный: {LOG_FILE})[/]",
                    box=box.SIMPLE,
                    padding=(0, 1),
                )
            )

        # Разделитель
        console.print(
            "[dim]─" * 70 + "[/]",
            highlight=False,
        )
        console.print(
            "[dim]Ctrl+C для остановки • "
            f"Следующее сканирование через {config.SCAN_INTERVAL_SECONDS}с[/]"
        )
