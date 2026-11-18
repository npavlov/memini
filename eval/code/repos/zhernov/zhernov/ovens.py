"""Расписание печей: какие партии в какую печь и в каком порядке."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Партия:
    изделие: str
    противни: int
    температура_c: int
    минуты: int


@dataclass
class Печь:
    номер: int
    вместимость: int  # противней за одну посадку
    предел_c: int
    очередь: list[Партия] = field(default_factory=list)

    def загрузка_минут(self) -> int:
        return sum(п.минуты for п in self.очередь)


class OvenOverloaded(Exception):
    """Партия не помещается ни в одну печь."""


def plan_batches(партии: list[Партия], печи: list[Печь]) -> list[Печь]:
    """Раскладывает партии по печам: горячие раньше, чтобы печь между партиями только остывала.

    Каждая партия целиком идёт в наименее загруженную печь, где хватает вместимости и предела
    температуры. Партия не делится между печами."""
    for партия in sorted(партии, key=lambda п: -п.температура_c):
        годные = [
            п
            for п in печи
            if п.вместимость >= партия.противни and п.предел_c >= партия.температура_c
        ]
        if not годные:
            raise OvenOverloaded(f"партия {партия.изделие} не помещается ни в одну печь")
        min(годные, key=lambda п: (п.загрузка_минут(), п.номер)).очередь.append(партия)
    return печи


def preheat_minutes(current_c: int, target_c: int, degrees_per_minute: float = 8.0) -> int:
    """Сколько минут разогревать печь до нужной температуры."""
    if target_c <= current_c:
        return 0
    return round((target_c - current_c) / degrees_per_minute)
