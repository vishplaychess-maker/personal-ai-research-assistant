/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

// Vite `?raw` imports keep this test free of Node builtins, so it typechecks under
// the DOM-only tsconfig that `npm run build` uses (no @types/node in this project).
import indexHtml from "../../index.html?raw";
import viteConfigSource from "../../vite.config.ts?raw";

/** D6B-R6 regression — the web manifest declaration must have exactly ONE owner.
 *
 * Proven defect: `index.html` hand-declared
 *   <link rel="manifest" href="/manifest.webmanifest" />
 * while `vite-plugin-pwa` already injects that same link at build time
 * (verified: a production `dist/index.html` carries the link TWICE). In the dev
 * server `devOptions` is disabled and `public/manifest.webmanifest` does not
 * exist, so `/manifest.webmanifest` falls through to the SPA fallback and Chrome
 * receives `index.html` (text/html) where it expects a web manifest, producing
 * "Manifest: Line: 1, column: 1, Syntax error."
 *
 * The plugin is the single source of truth, so the hand-written duplicate must go.
 */


describe("web manifest declaration (D6B-R6)", () => {
  it("declares no hand-written <link rel=manifest>; the PWA plugin owns it", () => {
    const handWritten = indexHtml.match(/<link[^>]*rel=["']manifest["'][^>]*>/gi) ?? [];
    expect(handWritten).toEqual([]);
  });

  it("still exposes the plugin manifest configuration (not a manifest removal)", () => {
    expect(viteConfigSource).toMatch(/VitePWA\(/);
    expect(viteConfigSource).toMatch(/manifest:\s*\{/);
    expect(viteConfigSource).toMatch(/name:\s*"Thunder AI"/);
  });
});
