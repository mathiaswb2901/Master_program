import { describe, expect, it } from "vitest";

import { relativeTime, relativeTimePhrase } from "./relativeTime";

const NOW = 1_700_000_000;

describe("relativeTime", () => {
  it("buckets by the unit that reads fastest", () => {
    expect(relativeTime(NOW, NOW)).toBe("now");
    expect(relativeTime(NOW - 59, NOW)).toBe("now");
    expect(relativeTime(NOW - 60, NOW)).toBe("1m");
    expect(relativeTime(NOW - 3599, NOW)).toBe("59m");
    expect(relativeTime(NOW - 3600, NOW)).toBe("1h");
    expect(relativeTime(NOW - 86_399, NOW)).toBe("23h");
    expect(relativeTime(NOW - 86_400, NOW)).toBe("1d");
  });

  it("clamps a future timestamp instead of rendering a negative age", () => {
    // The server stamps with its own clock; a skewed browser must not show "-2m".
    expect(relativeTime(NOW + 120, NOW)).toBe("now");
  });

  it("has a sentence form for the file bar", () => {
    expect(relativeTimePhrase(NOW, NOW)).toBe("just now");
    expect(relativeTimePhrase(NOW - 300, NOW)).toBe("5m ago");
  });
});
