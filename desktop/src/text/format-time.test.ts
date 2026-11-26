import { describe, expect, it } from "vitest";

import { formatAgo, formatClock, formatDuration, formatInstant } from "./format-time";

describe("formatDuration", () => {
  it.each([
    [0, "0 s"],
    [42.4, "42 s"],
    [60, "1 min"],
    [3599, "59 min"],
    [3600, "1 h"],
    [3 * 3600 + 20 * 60, "3 h 20 min"],
    [2 * 86_400 + 4 * 3600 + 59, "2 d 4 h"],
  ])("shows %d seconds in its two largest units as %s", (seconds, shown) => {
    expect(formatDuration(seconds)).toBe(shown);
  });

  it("shows a dash when there is no value", () => {
    expect(formatDuration(null)).toBe("—");
  });

  it("shows zero for a negative span", () => {
    expect(formatDuration(-5)).toBe("0 s");
  });
});

describe("formatInstant", () => {
  const format = { locale: "en-GB", timeZone: "UTC" };

  it("shows an ISO instant in the given locale and time zone", () => {
    expect(formatInstant("2026-11-23T08:05:00Z", format)).toBe("23 Nov 2026, 08:05");
  });

  it.each([[null], ["<b>soon</b>"], [""]])("shows a dash for %j, which is not a date", (value) => {
    expect(formatInstant(value, format)).toBe("—");
  });
});

describe("formatAgo", () => {
  const now = Date.parse("2026-11-23T12:00:00Z");

  it.each([
    ["2026-11-23T11:57:00Z", "3 min ago"],
    ["2026-11-21T12:00:00Z", "2 d ago"],
  ])("measures %s against the clock it is given", (instant, shown) => {
    expect(formatAgo(instant, now)).toBe(shown);
  });

  it("shows an instant after the clock as just now", () => {
    expect(formatAgo("2026-11-23T12:00:05Z", now)).toBe("just now");
  });

  it("shows a dash when there is no instant", () => {
    expect(formatAgo(null, now)).toBe("—");
  });
});

describe("formatClock", () => {
  const format = { locale: "en-GB", timeZone: "UTC" };
  const noon = Date.parse("2026-11-26T12:00:00Z");

  it("shows the time alone on the same day, and the date with it on another", () => {
    expect(formatClock("2026-11-26T07:37:00Z", noon, format)).toBe("07:37");
    expect(formatClock("2026-11-25T07:37:00Z", noon, format)).toMatch(/^25 Nov 2026,? 07:37$/u);
  });

  it("shows a dash for no instant, or one that is not a date", () => {
    expect(formatClock(null, noon, format)).toBe("—");
    expect(formatClock("soon", noon, format)).toBe("—");
  });
});
