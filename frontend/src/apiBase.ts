/**
 * Phase 5 — Optional API base URL for cloud deployment.
 *
 * VITE_API_BASE_URL is unset in local dev: API_BASE is "" and every request
 * stays relative ("/api/...") exactly as before. In a cross-origin cloud
 * setup (frontend on Vercel, backend elsewhere, no /api rewrite) set
 * VITE_API_BASE_URL to the backend origin, e.g. "https://your-app.onrender.com".
 *
 * NOTE: if you use the Vercel /api rewrite (frontend/vercel.json), keep this
 * unset — the same-site rewrite keeps auth cookies and CSRF working.
 */
export const API_BASE: string = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

/** Prepend the optional API base to a relative API path. */
export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}
