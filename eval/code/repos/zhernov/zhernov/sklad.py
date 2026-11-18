"""Склад муки: приход, списание на замесы и сигнал о малом остатке."""

from dataclasses import dataclass, field
from datetime import date

МИНИМАЛЬНЫЙ_ОСТАТОК_КГ = 150.0


class НедостаточноМуки(Exception):
    """На складе меньше муки, чем нужно списать."""


@dataclass
class Склад:
    остатки: dict[str, float] = field(default_factory=dict)
    журнал: list[tuple[date, str, float]] = field(default_factory=list)

    def принять(self, сорт: str, кг: float, день: date) -> None:
        """Оприходовать поставку муки."""
        self.остатки[сорт] = self.остатки.get(сорт, 0.0) + кг
        self.журнал.append((день, сорт, кг))

    def списать_муку(self, сорт: str, кг: float, день: date) -> float:
        """Списать муку на замес и вернуть остаток; уйти в минус нельзя."""
        есть = self.остатки.get(сорт, 0.0)
        if кг > есть:
            raise НедостаточноМуки(f"{сорт}: нужно {кг} кг, есть {есть} кг")
        self.остатки[сорт] = есть - кг
        self.журнал.append((день, сорт, -кг))
        return self.остатки[сорт]

    def мало_муки(self) -> list[str]:
        """Сорта, которых осталось меньше минимального запаса: их пора заказать."""
        return sorted(с for с, кг in self.остатки.items() if кг < МИНИМАЛЬНЫЙ_ОСТАТОК_КГ)
