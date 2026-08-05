/**
 * A stylesheet reader, just big enough for the motion budget.
 *
 * Shared by `motion.test.ts` (source stylesheets, vitest) and `motion.spec.ts`
 * (the production bundle, Playwright) so both halves of the conformance test
 * agree on what "a rule" and "an animated property" mean. Deliberately not a
 * CSS parser dependency: the questions asked here are "which selector declared
 * a transition" and "which properties does that value name", and a hundred
 * lines answers both for every stylesheet this repo ships or bundles.
 */

/**
 * Properties DESIGN.md §5 allows an animation to touch.
 *
 * `transform` and `opacity` are composited — the browser can run them without
 * style, layout or paint. The four colour properties are paint-only, which is
 * cheap at chrome scale and is what the tint channel spends. Everything else
 * either triggers layout or is not motion.
 */
export const ANIMATABLE: ReadonlySet<string> = new Set([
  "transform",
  "opacity",
  "background-color",
  "border-color",
  "color",
  "outline-color",
]);

export interface CssRule {
  /** Selector text, whitespace collapsed. At-rule preludes arrive as-is. */
  selector: string;
  /** Declarations, `;`-separated and not otherwise parsed. */
  body: string;
}

/**
 * Split a stylesheet into flat `selector { body }` pairs.
 *
 * At-rules (`@media`, `@supports`, `@keyframes`) are descended into rather than
 * reported, so a rule inside a media query is checked exactly like one outside
 * it — which is the point: the reduced-motion block is where a travel animation
 * would most plausibly hide.
 */
export function rules(css: string): CssRule[] {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const blocks: CssRule[] = [];
  let depth = 0;
  let open = 0;
  let selectorStart = 0;
  for (let i = 0; i < clean.length; i++) {
    const character = clean[i];
    if (character === "{") {
      if (depth === 0) open = i;
      depth++;
    } else if (character === "}") {
      depth = Math.max(0, depth - 1);
      if (depth !== 0) continue;
      blocks.push({
        selector: clean.slice(selectorStart, open).trim().replace(/\s+/g, " "),
        body: clean.slice(open + 1, i),
      });
      selectorStart = i + 1;
    }
  }
  return blocks.flatMap((block) => (block.body.includes("{") ? rules(block.body) : [block]));
}

/**
 * Tokens in a `transition` shorthand that are timings, easings or keywords
 * rather than property names.
 */
const NOT_A_PROPERTY =
  /^(?:[\d.]|-\d)|^(?:ease|ease-in|ease-out|ease-in-out|linear|step-start|step-end|steps|cubic-bezier|infinite|alternate|alternate-reverse|reverse|normal|forwards|backwards|both|running|paused|initial|inherit|unset|revert|var)$/;

/**
 * The properties a `transition` value animates.
 *
 * Parenthesised bodies are blanked first — `linear(0, .5 20%, 1)` and
 * `cubic-bezier(…)` both hold commas, and splitting on those would tear one
 * layer into several and turn easing arguments into "properties".
 */
export function animatedProperties(value: string): string[] {
  let flat = value.replace(/!\s*important/gi, "");
  for (let i = 0; i < 8; i++) flat = flat.replace(/\([^()]*\)/g, "()");
  return flat
    .split(",")
    .flatMap((layer) => layer.trim().split(/\s+/))
    .map((token) => token.replace(/\(\)$/, "").trim().toLowerCase())
    .filter((token) => token !== "" && !NOT_A_PROPERTY.test(token));
}

export interface MotionDeclaration extends CssRule {
  properties: string[];
  /** The declaration's value, for an error message someone can act on. */
  raw: string;
}

/** Every `transition` / `transition-property` declaration in a stylesheet. */
export function motionDeclarations(css: string): MotionDeclaration[] {
  const out: MotionDeclaration[] = [];
  for (const rule of rules(css)) {
    for (const declaration of rule.body.split(";")) {
      const colon = declaration.indexOf(":");
      if (colon < 0) continue;
      const property = declaration.slice(0, colon).trim().toLowerCase();
      if (property !== "transition" && property !== "transition-property") continue;
      const value = declaration.slice(colon + 1).trim();
      out.push({ ...rule, properties: animatedProperties(value), raw: value });
    }
  }
  return out;
}
