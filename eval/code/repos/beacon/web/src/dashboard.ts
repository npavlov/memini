// Панель состояния маяка: последние показания и время работы агента.

export interface BeaconStatus {
  name: string;
  batteryPercent: number;
  turnsPerMinute: number;
  visibilityMeters: number;
  hornOn: boolean;
  uptimeSeconds: number;
}

// formatUptime показывает время работы агента как «3 д 4 ч 5 мин».
export function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts: string[] = [];
  if (days > 0) parts.push(`${days} д`);
  if (hours > 0) parts.push(`${hours} ч`);
  parts.push(`${minutes} мин`);
  return parts.join(" ");
}

// renderStatusPanel собирает HTML-разметку панели одного маяка.
export function renderStatusPanel(status: BeaconStatus): string {
  const horn = status.hornOn ? "горн включён" : "горн выключен";
  return [
    `<section class="beacon">`,
    `<h2>${escapeHtml(status.name)}</h2>`,
    `<p>Заряд: ${status.batteryPercent}%</p>`,
    `<p>Обороты: ${status.turnsPerMinute.toFixed(1)} в минуту</p>`,
    `<p>${horn}</p>`,
    `<p>Работает: ${formatUptime(status.uptimeSeconds)}</p>`,
    `</section>`,
  ].join("\n");
}

// escapeHtml экранирует текст перед вставкой в разметку.
export function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
