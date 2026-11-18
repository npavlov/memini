"""Расчёт закваски на завтрашние замесы."""

from dataclasses import dataclass

# Доля закваски от массы муки в зависимости от температуры в цехе, °C: чем холоднее, тем больше.
ДОЛЯ_ПО_ТЕМПЕРАТУРЕ = [
    (18.0, 0.40),
    (22.0, 0.30),
    (26.0, 0.20),
]


@dataclass(frozen=True)
class Замес:
    изделие: str
    мука_кг: float


def starter_share(temperature_c: float) -> float:
    """Доля закваски от муки: чем холоднее в цехе, тем больше закваски нужно.

    Берётся первая строка таблицы, чей порог не ниже температуры; выше последнего порога
    действует последняя доля."""
    if temperature_c < 0:
        raise ValueError("в цехе не бывает мороза")
    for limit, share in ДОЛЯ_ПО_ТЕМПЕРАТУРЕ:
        if temperature_c <= limit:
            return share
    return ДОЛЯ_ПО_ТЕМПЕРАТУРЕ[-1][1]


def starter_mass(замесы: list[Замес], temperature_c: float) -> float:
    """Сколько килограммов закваски поставить с вечера на все замесы завтрашнего дня.

    Масса — доля от общей муки замесов, округлённая до ста граммов."""
    мука = 0.0
    for замес in замесы:
        if замес.мука_кг <= 0:
            raise ValueError(f"в замесе {замес.изделие} нет муки")
        мука += замес.мука_кг
    return round(мука * starter_share(temperature_c), 1)


def feeding(starter_kg: float) -> tuple[float, float]:
    """Подкормка закваски на утро: мука и вода поровну, каждой по половине массы закваски."""
    half = starter_kg / 2
    return half, half
