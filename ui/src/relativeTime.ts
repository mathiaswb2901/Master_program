/**
 * Coarse "how long ago" for unix-second timestamps. Pure and dependency-free —
 * unit-tested in `relativeTime.test.ts`.
 *
 * Deliberately coarse: these label a session row and a file bar, where the
 * question is "recent or not", never "exactly when". Future timestamps (a clock
 * skew between the server's `time.time()` and the browser) clamp to "now"
 * rather than render a negative age.
 */

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** Compact form for dense rows: "now", "5m", "3h", "2d". */
export function relativeTime(unixSeconds: number, now: number = Date.now() / 1000): string {
  const delta = Math.max(0, now - unixSeconds);
  if (delta < MINUTE) return "now";
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h`;
  return `${Math.floor(delta / DAY)}d`;
}

/** Sentence form for prose: "just now", "5m ago". */
export function relativeTimePhrase(unixSeconds: number, now: number = Date.now() / 1000): string {
  const compact = relativeTime(unixSeconds, now);
  return compact === "now" ? "just now" : `${compact} ago`;
}
