"""Ежедневный отчёт технологу: выпуск, брак и остаток муки."""

from collections import Counter
from datetime import date

from zhernov.sklad import Склад


def defect_rate(выпущено: int, брак: int) -> float:
    """Доля брака в процентах от выпуска, с одним знаком после запятой."""
    if выпущено == 0:
        return 0.0
    return round(100 * брак / выпущено, 1)


def daily_report(день: date, выпуск: Counter[str], брак: Counter[str], склад: Склад) -> str:
    """Текст отчёта за день: по строке на изделие, затем остатки муки по сортам."""
    lines = [f"Отчёт за {день:%d.%m.%Y}"]
    for изделие in sorted(выпуск):
        процент = defect_rate(выпуск[изделие], брак[изделие])
        lines.append(f"{изделие}: {выпуск[изделие]} шт., брак {процент}%")
    lines.append("Остаток муки:")
    for сорт, кг in sorted(склад.остатки.items()):
        lines.append(f"  {сорт}: {кг:.0f} кг")
    return "\n".join(lines)
