/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

// Vite `?raw` import keeps this test free of Node builtins, so it typechecks under
// the DOM-only tsconfig that `npm run build` uses.
import indexHtml from "../../index.html?raw";

/** D6C-J6 RED regression — an inline script in index.html does not parse.
 *
 * PROVEN DEFECT -- NOT FIXED. No production code was changed.
 *
 * Stage 0A of the J6 plan attributed the previously unattributed browser console
 * error "Unexpected token ')'". Captured at load time with a pre-load listener:
 *
 *   kind=error  filename=http://127.0.0.1:<port>/   (the DOCUMENT, not a .js file)
 *   lineno=26   colno=150   stack="SyntaxError: Unexpected token ')'"
 *
 * The served document's inline theme script is:
 *
 *   (function(){try{if(localStorage.getItem("research-assistant-theme")==="dark")
 *     {document.documentElement.classList.add("dark");}}catch(e){})();
 *
 * It has 4 opening braces and 3 closing braces: the function body's closing brace
 * is missing before ")()", so the script is a SyntaxError and NEVER RUNS. The
 * consequences are real and not dev-only: dark-mode flash prevention is dead, and
 * every page load in every environment logs a syntax error, which fails the J6
 * acceptance criterion of zero syntax errors.
 *
 * The fix is one character in index.html (catch(e){})(); -> catch(e){}})(); ) and
 * is intentionally NOT applied here: frontend/** changes are outside this task's
 * allowed boundary and production-fix authorization was requested first.
 */

function inlineScripts(html: string): string[] {
  const out: string[] = [];
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) out.push(m[1]);
  return out;
}

describe("index.html inline scripts (D6C-J6)", () => {
  it("has at least one inline script", () => {
    expect(inlineScripts(indexHtml).length).toBeGreaterThan(0);
  });

  it("every inline script is syntactically valid JavaScript (RED: theme script is not)", () => {
    const failures: string[] = [];
    for (const src of inlineScripts(indexHtml)) {
      try {
        // Classic inline script: parsing it is exactly what the browser does.
        new Function(src);
      } catch (err) {
        failures.push(src.slice(0, 60) + " -> " + String(err));
      }
    }
    expect(failures).toEqual([]);
  });

  it("every inline script has balanced braces (RED: theme script is 4 open / 3 close)", () => {
    const unbalanced = inlineScripts(indexHtml)
      .map((src) => ({
        opens: (src.match(/\{/g) ?? []).length,
        closes: (src.match(/\}/g) ?? []).length,
        head: src.slice(0, 60),
      }))
      .filter((s) => s.opens !== s.closes);
    expect(unbalanced).toEqual([]);
  });
});
