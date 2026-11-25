const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const NOTHING = "—";

/** A span of seconds in its two largest units: `42 s`, `5 min`, `3 h 20 min`, `2 d 4 h`. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) {
    return NOTHING;
  }
  const whole = Math.max(0, Math.round(seconds));
  if (whole < MINUTE) {
    return `${String(whole)} s`;
  }
  if (whole < HOUR) {
    return `${String(Math.floor(whole / MINUTE))} min`;
  }
  if (whole < DAY) {
    return joinAmounts(
      { count: Math.floor(whole / HOUR), unit: "h" },
      { count: Math.floor((whole % HOUR) / MINUTE), unit: "min" },
    );
  }
  return joinAmounts(
    { count: Math.floor(whole / DAY), unit: "d" },
    { count: Math.floor((whole % DAY) / HOUR), unit: "h" },
  );
}

interface Amount {
  readonly count: number;
  readonly unit: string;
}

function joinAmounts(major: Amount, minor: Amount): string {
  const first = `${String(major.count)} ${major.unit}`;
  return minor.count === 0 ? first : `${first} ${String(minor.count)} ${minor.unit}`;
}

export interface InstantFormat {
  readonly locale?: string;
  readonly timeZone?: string;
}

/** An ISO instant in the reader's locale and time zone; a dash for none or one that is not a date. */
export function formatInstant(iso: string | null, format: InstantFormat = {}): string {
  const time = iso === null ? Number.NaN : Date.parse(iso);
  if (Number.isNaN(time)) {
    return NOTHING;
  }
  return new Intl.DateTimeFormat(format.locale, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: format.timeZone,
  }).format(new Date(time));
}

/** How long ago an ISO instant was, against a clock that is passed in: `3 min ago`. */
export function formatAgo(iso: string | null, nowMs: number): string {
  const time = iso === null ? Number.NaN : Date.parse(iso);
  if (Number.isNaN(time)) {
    return NOTHING;
  }
  const seconds = (nowMs - time) / 1000;
  return seconds < 0 ? "just now" : `${formatDuration(seconds)} ago`;
}
