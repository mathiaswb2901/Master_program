/**
 * The Specs panel — the body behind `reconcileSpec.ts`'s dynamic `import()`.
 *
 * One row per `.workbench/reconcile/<name>.toml`: what it proves, whether it may
 * run, what its last run said, and — for an approved spec — **the list of files
 * that approval stands for**, rendered verbatim.
 *
 * That last part is the panel's job and not decoration. The copy this surface
 * uses is *this spec, running exactly this code, on this machine, until either
 * changes*, and a sentence like that is only worth saying next to a list a
 * person can check. An approval that authorises code it never hashed is a trust
 * prompt's shadow; an approval whose coverage a user cannot read is the same
 * shadow one step further along.
 *
 * Nothing here can make a spec run on its own. Approve echoes the digest **this
 * row was rendered with**, so a spec whose bytes moved between the render and
 * the click is refused by the server (409) and the row is re-read.
 *
 * Every colour is a DESIGN.md token via `styles/reconcileSpec.css`, and the
 * status pill reuses `.wb-pill` from the Review panel's sheet so a "pass" here
 * and a "pass" there are the same green by construction.
 */

import { memo, useEffect, type CSSProperties, type ReactElement } from "react";

import {
  attentionTitle,
  needsAttention,
  orderSpecs,
  outcomeVisual,
  statusVisual,
  summaryOf,
  useSpecStore,
} from "../reconcileSpec";
import type { CoveredSource, SpecState } from "../types";

import "../styles/review.css";
import "../styles/reconcileSpec.css";

/** The pill, coloured by the shared semantic mapping. Colour is never the only
 * signal — the word rides beside it (DESIGN §7). */
function Pill({ visual }: { visual: { token: string; bg: string; label: string } }): ReactElement {
  const style = {
    "--pill-color": `var(${visual.token})`,
    "--pill-bg": `var(${visual.bg})`,
  } as CSSProperties;
  return (
    <span className="wb-pill" style={style}>
      <span className="wb-pill-dot" aria-hidden="true" />
      {visual.label}
    </span>
  );
}

/** How a covered file entered the approval, in a word a person reads. */
const ORIGIN_WORD: Record<CoveredSource["origin"], string> = {
  spec: "spec",
  callable: "named",
  imported: "imported",
};

/** The receipt behind the approval sentence. `imported` entries are the ones a
 * previous run actually loaded — which is why the list can grow after run 1, and
 * why the panel shows it rather than describing it. */
function Covered({ covered }: { covered: readonly CoveredSource[] }): ReactElement | null {
  if (covered.length === 0) return null;
  return (
    <ul className="wb-spec-covered" aria-label="Files this approval covers">
      {covered.map((source) => (
        <li key={source.path}>
          <span className="wb-spec-covered-origin">{ORIGIN_WORD[source.origin]}</span>
          <span className="wb-spec-covered-path" title={source.path}>
            {source.path}
          </span>
        </li>
      ))}
    </ul>
  );
}

function SpecCard({ spec }: { spec: SpecState }): ReactElement {
  const approve = useSpecStore((state) => state.approve);
  const revoke = useSpecStore((state) => state.revoke);
  const run = useSpecStore((state) => state.run);
  const armed = spec.status === "approved";
  const lastRun = spec.last_run;
  return (
    <article className="wb-spec-card" data-spec={spec.name}>
      <header className="wb-spec-head">
        <h3 className="wb-spec-name">{spec.name}</h3>
        <span className="wb-spec-workbook" title={spec.path}>
          {spec.workbook || spec.path}
        </span>
        <Pill visual={statusVisual(spec.status)} />
      </header>
      <p className="wb-spec-detail">{spec.detail}</p>
      {lastRun !== null && (
        <p className="wb-spec-run">
          <Pill visual={outcomeVisual(lastRun.outcome)} />{" "}
          {lastRun.trigger === "watcher" ? "on save" : "run by hand"} — {lastRun.detail}
        </p>
      )}
      {spec.approval !== null && <Covered covered={spec.approval.covered} />}
      <div className="wb-spec-actions">
        {spec.status !== "invalid" && !armed && (
          <button
            type="button"
            className="wb-spec-action is-primary"
            onClick={() => void approve(spec.name)}
          >
            {spec.status === "stale" ? "Approve again" : "Approve"}
          </button>
        )}
        {spec.approval !== null && (
          <button
            type="button"
            className="wb-spec-action"
            onClick={() => void revoke(spec.name)}
          >
            Revoke
          </button>
        )}
        {armed && (
          <button type="button" className="wb-spec-action" onClick={() => void run(spec.name)}>
            Run now
          </button>
        )}
      </div>
    </article>
  );
}

/**
 * The panel. Reading it runs nothing — opening a workspace with twenty specs in
 * it spawns no process and prompts for nothing, which is a property of the
 * server and a promise this surface must not quietly break by, say, running one
 * to populate a row.
 */
export const SpecPanel = memo(function SpecPanel(): ReactElement {
  const specs = useSpecStore((state) => state.specs);
  const detail = useSpecStore((state) => state.detail);
  const hydrated = useSpecStore((state) => state.hydrated);
  const error = useSpecStore((state) => state.error);
  useEffect(() => {
    useSpecStore.getState().init();
  }, []);
  const rows = orderSpecs(Object.values(specs));
  return (
    <div className="wb-specs">
      <div className="wb-specs-body">
        {error !== null && <p className="wb-specs-error">{error}</p>}
        {rows.length === 0 ? (
          <p className="wb-specs-empty">
            {hydrated ? detail || "No reconciliation specs." : "Loading…"}
          </p>
        ) : (
          <>
            <p className="wb-specs-intro" title={attentionTitle(rows)}>
              {rows.filter(needsAttention).length === 0
                ? "Every approved spec is passing. Save a workbook and its spec re-runs itself."
                : detail}
            </p>
            {rows.map((spec) => (
              <SpecCard key={spec.name} spec={spec} />
            ))}
          </>
        )}
      </div>
    </div>
  );
});

/** Named for the row's own summary word, so a test can assert the panel and the
 * chip agree about what a spec is saying. */
export { summaryOf };

export default SpecPanel;
