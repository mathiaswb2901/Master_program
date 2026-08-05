/**
 * The one global the node test environment is missing.
 *
 * `@xterm/addon-fit` ships a UMD bundle whose wrapper reads `self` at module
 * scope, so importing the Terminal tool's module — which the registry tests do,
 * because they assert over the *real* `TOOLS` — throws before a single
 * assertion runs. Aliasing `self` to `globalThis` costs three lines; the
 * alternative was a jsdom dependency for one identifier.
 *
 * Everything heavier (Monaco's bundle, the store's `document` read at import)
 * is mocked per test file, where the stub is next to the assertion that needs it.
 */

(globalThis as { self?: unknown }).self ??= globalThis;
