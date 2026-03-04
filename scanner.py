"""
Momentum Scalper Bot — Сканер рынка
Сканирование и фильтрация USDT Perpetual пар по объёму
"""

import time
from rich.console import Console

import config
from exchange import BybitExchange

console = Console()


class MarketScanner:
    """Сканер рынка — выбирает лучшие пары для скальпинга."""

    def __init__(self, exchange: BybitExchange):
        self.exchange = exchange
        self._pairs_cache: list = []
        self._instruments_cache: dict = {}
        self._last_refresh: float = 0

    def refresh_pairs(self, force: bool = False) -> list[str]:
        """
        Обновить список пар. Кешируется на SCANNER_REFRESH_INTERVAL.
        Возвращает отсортированный по объёму список символов.
        """
        now = time.time()
        if (
            not force
            and self._pairs_cache
            and (now - self._last_refresh) < config.SCANNER_REFRESH_INTERVAL
        ):
            return self._pairs_cache

        console.print("[cyan]🔍 Сканирование рынка...[/]")

        # Получаем инструменты для фильтрации
        instruments = self.exchange.get_instruments_info()
        self._instruments_cache = {}
        valid_symbols = set()
        for inst in instruments:
            symbol = inst.get("symbol", "")
            status = inst.get("status", "")
            quote = inst.get("quoteCoin", "")
            # Только USDT пары со статусом Trading
            if quote == config.QUOTE_CURRENCY and status == "Trading":
                valid_symbols.add(symbol)
                self._instruments_cache[symbol] = {
                    "minQty": float(inst["lotSizeFilter"]["minOrderQty"]),
                    "maxQty": float(inst["lotSizeFilter"]["maxOrderQty"]),
                    "qtyStep": inst["lotSizeFilter"]["qtyStep"],
                    "minPrice": inst["priceFilter"]["minPrice"],
                    "tickSize": inst["priceFilter"]["tickSize"],
                }

        # Получаем тикеры и фильтруем по объёму
        tickers = self.exchange.get_tickers()
        scored_pairs = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if symbol not in valid_symbols:
                continue
            try:
                volume_24h = float(t.get("turnover24h", 0))
            except (ValueError, TypeError):
                continue
            if volume_24h < config.MIN_24H_VOLUME_USDT:
                continue
            scored_pairs.append((symbol, volume_24h))

        # Сортируем по объёму (больше = лучше)
        scored_pairs.sort(key=lambda x: x[1], reverse=True)

        # Берём топ N пар
        self._pairs_cache = [
            s[0] for s in scored_pairs[: config.TOP_PAIRS_LIMIT]
        ]
        self._last_refresh = now

        console.print(
            f"[green]✓ Найдено {len(self._pairs_cache)} пар "
            f"(из {len(valid_symbols)} доступных)[/]"
        )
        return self._pairs_cache

    def get_instrument_info(self, symbol: str) -> dict:
        """Получить кешированную информацию об инструменте."""
        return self._instruments_cache.get(symbol, {})

    def get_pairs(self) -> list[str]:
        """Получить текущий список пар (из кеша или обновить)."""
        if not self._pairs_cache:
            return self.refresh_pairs(force=True)
        return self._pairs_cache
