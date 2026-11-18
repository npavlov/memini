// Тревоги панели: сортировка по важности и подавление повторов.

export type Level = "info" | "warning" | "alarm";

export interface Alert {
  beacon: string;
  level: Level;
  message: string;
  at: Date;
}

const ORDER: Record<Level, number> = { alarm: 0, warning: 1, info: 2 };

// sortAlerts ставит первыми самые важные тревоги, а внутри одного уровня — самые свежие.
export function sortAlerts(alerts: Alert[]): Alert[] {
  return [...alerts].sort(
    (a, b) => ORDER[a.level] - ORDER[b.level] || b.at.getTime() - a.at.getTime(),
  );
}

// dedupeAlerts убирает повтор той же тревоги того же маяка внутри окна (по умолчанию 15 минут).
export function dedupeAlerts(alerts: Alert[], windowMinutes = 15): Alert[] {
  const lastSeen = new Map<string, number>();
  const kept: Alert[] = [];
  for (const alert of [...alerts].sort((a, b) => a.at.getTime() - b.at.getTime())) {
    const key = `${alert.beacon}|${alert.message}`;
    const previous = lastSeen.get(key);
    if (previous !== undefined && alert.at.getTime() - previous < windowMinutes * 60_000) {
      continue;
    }
    lastSeen.set(key, alert.at.getTime());
    kept.push(alert);
  }
  return kept;
}
