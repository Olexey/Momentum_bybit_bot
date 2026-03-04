"""
Momentum Scalper Bot — Подключение к Bybit
Обёртка над pybit HTTP для Demo API v5
"""

import time
import logging
import threading
from typing import Optional
from pybit.unified_trading import HTTP
from rich.console import Console

import config

console = Console()
logger = logging.getLogger("exchange")


class BybitExchange:
    """Обёртка над Bybit API v5 (Demo режим)."""

    def __init__(self):
        self.session = HTTP(
            testnet=config.TESTNET,
            demo=config.DEMO_MODE,
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
        )
        self._leverage_cache: set = set()
        self._time_offset: float = 0.0

        # ── Rate Limiter ──
        self._rate_lock = threading.Lock()
        self._request_times: list[float] = []
        self._max_requests_per_sec = 10

        # ── Balance Cache ──
        self._balance_cache: float = 0.0
        self._balance_cache_time: float = 0.0
        self._balance_cache_ttl: float = 15.0  # секунд

        # Синхронизация времени с биржей при старте
        self.sync_time()
        console.print("[bold green]✓[/] Подключение к Bybit Demo API установлено")

    # ─────────────────────────────────────────────────────────
    # Rate Limiter
    # ─────────────────────────────────────────────────────────
    def _rate_limit(self):
        """Ожидание если превышен лимит запросов (thread-safe)."""
        with self._rate_lock:
            now = time.time()
            # Убираем запросы старше 1 секунды
            self._request_times = [
                t for t in self._request_times if now - t < 1.0
            ]
            if len(self._request_times) >= self._max_requests_per_sec:
                sleep_time = 1.0 - (now - self._request_times[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self._request_times.append(time.time())

    def _api_call_with_retry(self, func, *args, max_retries=3, **kwargs):
        """API вызов с rate limit и exponential backoff."""
        for attempt in range(max_retries):
            self._rate_limit()
            try:
                return func(*args, **kwargs)
            except Exception as e:
                err_msg = str(e)
                # Rate limit ошибка от Bybit
                if "Too many" in err_msg or "429" in err_msg:
                    wait = (2 ** attempt) * 0.5
                    logger.warning(f"Rate limit hit, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                raise  # Другая ошибка — пробрасываем
        return func(*args, **kwargs)  # Последняя попытка без catch

    # ─────────────────────────────────────────────────────────
    # Время биржи
    # ─────────────────────────────────────────────────────────
    def get_server_time(self) -> float:
        """Получить серверное время Bybit (в секундах, epoch)."""
        try:
            resp = self.session.get_server_time()
            server_ms = int(resp["result"]["timeSecond"])
            return float(server_ms)
        except Exception:
            try:
                resp = self.session.get_server_time()
                server_ms = int(resp["result"]["timeNano"]) / 1_000_000_000
                return server_ms
            except Exception as e:
                console.print(f"[yellow]⚠ Не удалось получить время биржи: {e}[/]")
                return time.time()

    def sync_time(self):
        """Синхронизация: вычислить offset между локальным и серверным временем."""
        try:
            local_before = time.time()
            server_time = self.get_server_time()
            local_after = time.time()
            local_mid = (local_before + local_after) / 2
            self._time_offset = server_time - local_mid
            offset_ms = abs(self._time_offset * 1000)
            if offset_ms > 1000:
                console.print(
                    f"[yellow]⚠ Время компьютера расходится с биржей на "
                    f"{self._time_offset:+.1f} сек ({offset_ms/1000:.1f}с)[/]"
                )
            else:
                console.print(
                    f"[green]✓ Время синхронизировано (offset: {self._time_offset:+.3f}с)[/]"
                )
        except Exception as e:
            console.print(f"[yellow]⚠ Ошибка синхронизации времени: {e}[/]")
            self._time_offset = 0.0

    def now(self) -> float:
        """Получить текущее время, синхронизированное с биржей (epoch seconds)."""
        return time.time() + self._time_offset

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """Безопасная конвертация в float (пустые строки, None)."""
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # ─────────────────────────────────────────────────────────
    # Баланс (с кешем)
    # ─────────────────────────────────────────────────────────
    def get_balance(self) -> float:
        """Получить доступный USDT баланс (кешируется на 15 сек)."""
        now = time.time()
        if (now - self._balance_cache_time) < self._balance_cache_ttl and self._balance_cache > 0:
            return self._balance_cache

        try:
            resp = self._api_call_with_retry(
                self.session.get_wallet_balance,
                accountType="UNIFIED",
                coin="USDT",
            )
            coins = resp["result"]["list"][0]["coin"]
            for c in coins:
                if c["coin"] == "USDT":
                    for field in ["availableToWithdraw", "walletBalance", "equity"]:
                        val = self._safe_float(c.get(field))
                        if val > 0:
                            self._balance_cache = val
                            self._balance_cache_time = now
                            return val
                    return 0.0
            return 0.0
        except Exception as e:
            console.print(f"[red]✗ Ошибка получения баланса: {e}[/]")
            return self._balance_cache if self._balance_cache > 0 else 0.0

    # ─────────────────────────────────────────────────────────
    # Тикеры
    # ─────────────────────────────────────────────────────────
    def get_tickers(self) -> list:
        """Получить тикеры всех linear пар."""
        try:
            resp = self.session.get_tickers(category=config.CATEGORY)
            return resp["result"]["list"]
        except Exception as e:
            console.print(f"[red]✗ Ошибка получения тикеров: {e}[/]")
            return []

    # ─────────────────────────────────────────────────────────
    # Свечи (Klines)
    # ─────────────────────────────────────────────────────────
    def get_klines(self, symbol: str, interval: str, limit: int = None) -> list:
        """Получить свечи для пары."""
        if limit is None:
            limit = config.KLINE_LIMIT
        try:
            resp = self.session.get_kline(
                category=config.CATEGORY,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            return resp["result"]["list"]
        except Exception as e:
            console.print(f"[red]✗ Ошибка klines {symbol}: {e}[/]")
            return []

    # ─────────────────────────────────────────────────────────
    # Информация о инструменте
    # ─────────────────────────────────────────────────────────
    def get_instruments_info(self) -> list:
        """Получить информацию обо всех linear инструментах."""
        try:
            all_instruments = []
            cursor = None
            while True:
                params = {
                    "category": config.CATEGORY,
                    "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor
                resp = self.session.get_instruments_info(**params)
                instruments = resp["result"]["list"]
                all_instruments.extend(instruments)
                cursor = resp["result"].get("nextPageCursor", "")
                if not cursor:
                    break
            return all_instruments
        except Exception as e:
            console.print(f"[red]✗ Ошибка instruments info: {e}[/]")
            return []

    # ─────────────────────────────────────────────────────────
    # Плечо и Margin Mode
    # ─────────────────────────────────────────────────────────
    def set_margin_mode(self, symbol: str) -> bool:
        """Установить margin mode (ISOLATED) для пары."""
        try:
            self.session.switch_margin_mode(
                category=config.CATEGORY,
                symbol=symbol,
                tradeMode=1 if config.MARGIN_MODE == "ISOLATED" else 0,
            )
            return True
        except Exception as e:
            err_msg = str(e)
            # Уже установлен — ОК
            if "110026" in err_msg or "not modified" in err_msg.lower():
                return True
            # Некоторые пары не поддерживают switch — пропускаем
            return True

    def set_leverage(self, symbol: str) -> bool:
        """Установить margin mode + плечо для пары."""
        if symbol in self._leverage_cache:
            return True

        # Сначала — Isolated margin
        self.set_margin_mode(symbol)

        # Пробуем плечо от желаемого к минимальному
        leverage_options = [config.LEVERAGE, 3, 2, 1]
        for lev in leverage_options:
            try:
                self.session.set_leverage(
                    category=config.CATEGORY,
                    symbol=symbol,
                    buyLeverage=str(lev),
                    sellLeverage=str(lev),
                )
                self._leverage_cache.add(symbol)
                if lev != config.LEVERAGE:
                    console.print(
                        f"[yellow]⚠ {symbol}: плечо x{lev} "
                        f"(x{config.LEVERAGE} не поддерживается)[/]"
                    )
                return True
            except Exception as e:
                err_msg = str(e)
                if "leverage not modified" in err_msg.lower() or "110043" in err_msg:
                    self._leverage_cache.add(symbol)
                    return True
                continue

        console.print(f"[red]✗ Не удалось установить плечо для {symbol}[/]")
        return False

    # ─────────────────────────────────────────────────────────
    # Ордера
    # ─────────────────────────────────────────────────────────
    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        take_profit: Optional[str] = None,
        stop_loss: Optional[str] = None,
    ) -> dict:
        """Разместить рыночный ордер с TP/SL."""
        try:
            params = {
                "category": config.CATEGORY,
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": qty,
                "timeInForce": "GTC",
            }
            if take_profit:
                params["takeProfit"] = take_profit
            if stop_loss:
                params["stopLoss"] = stop_loss
            resp = self.session.place_order(**params)
            return resp["result"]
        except Exception as e:
            console.print(f"[red]✗ Ошибка ордера {side} {symbol}: {e}[/]")
            return {}

    def close_position(self, symbol: str, side: str, qty: str) -> dict:
        """Закрыть позицию (обратный ордер)."""
        close_side = "Sell" if side == "Buy" else "Buy"
        try:
            params = {
                "category": config.CATEGORY,
                "symbol": symbol,
                "side": close_side,
                "orderType": "Market",
                "qty": qty,
                "reduceOnly": True,
                "timeInForce": "GTC",
            }
            resp = self.session.place_order(**params)
            return resp["result"]
        except Exception as e:
            console.print(f"[red]✗ Ошибка закрытия {symbol}: {e}[/]")
            return {}

    # ─────────────────────────────────────────────────────────
    # Позиции
    # ─────────────────────────────────────────────────────────
    def get_positions(self) -> list:
        """Получить все открытые позиции."""
        try:
            resp = self.session.get_positions(
                category=config.CATEGORY,
                settleCoin="USDT",
            )
            positions = resp["result"]["list"]
            # Фильтруем только реально открытые (size > 0)
            return [p for p in positions if self._safe_float(p.get("size")) > 0]
        except Exception as e:
            console.print(f"[red]✗ Ошибка позиций: {e}[/]")
            return []

    def set_trading_stop(
        self,
        symbol: str,
        take_profit: Optional[str] = None,
        stop_loss: Optional[str] = None,
        position_idx: int = 0,
    ) -> bool:
        """Обновить TP/SL для позиции."""
        try:
            params = {
                "category": config.CATEGORY,
                "symbol": symbol,
                "positionIdx": position_idx,
            }
            if take_profit:
                params["takeProfit"] = take_profit
            if stop_loss:
                params["stopLoss"] = stop_loss
            self.session.set_trading_stop(**params)
            return True
        except Exception as e:
            console.print(f"[yellow]⚠ Trading stop {symbol}: {e}[/]")
            return False

    # ─────────────────────────────────────────────────────────
    # Закрытый PnL
    # ─────────────────────────────────────────────────────────
    def get_closed_pnl(self, symbol: str, limit: int = 5) -> float:
        """Получить последний закрытый PnL для пары."""
        try:
            resp = self.session.get_closed_pnl(
                category=config.CATEGORY,
                symbol=symbol,
                limit=limit,
            )
            records = resp["result"]["list"]
            if records:
                # Берём самую свежую запись
                return self._safe_float(records[0].get("closedPnl"))
            return 0.0
        except Exception as e:
            console.print(f"[yellow]⚠ Closed PnL {symbol}: {e}[/]")
            return 0.0
