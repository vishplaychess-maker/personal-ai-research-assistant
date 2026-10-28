/**
 * Report Export — frontend download helper.
 *
 * POSTs to /api/export (bearer auth + double-submit CSRF, same conventions
 * as api.ts) and triggers a browser download of the returned document blob.
 * No external npm dependency needed (Blob + object URL do the job).
 */

import { getAccessToken, isTokenExpired, refreshAccessToken, getCsrfToken } from "./auth";
import { apiUrl } from "./apiBase";

export type ExportType = "research_report" | "chat" | "knowledge_graph";
export type ExportFormat = "pdf" | "docx";

export interface ExportPayload {
  type: ExportType;
  format: ExportFormat;
  title?: string;
  data: Record<string, unknown>;
}

/** Server-generated Content-Disposition is informational only; extension comes from the requested format. */
function buildFilename(disposition: string | null, format: ExportFormat): string {
  let name = `thunder-ai-export.${format}`;
  if (disposition) {
    const m = /filename="([^"]+)"/.exec(disposition);
    if (m?.[1]) name = m[1];
  }
  if (!name.toLowerCase().endsWith(`.${format}`)) {
    name = `${name}.${format}`;
  }
  return name;
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function getValidToken(): Promise<string | null> {
  const token = getAccessToken();
  if (token && !isTokenExpired(token)) return token;
  const refreshed = await refreshAccessToken();
  return refreshed ? getAccessToken() : null;
}

/**
 * Request a document export and download it.
 * Throws an Error with a user-readable message on failure.
 */
export async function exportDocument(payload: ExportPayload): Promise<void> {
  const token = await getValidToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const csrf = getCsrfToken();
  if (csrf) headers["X-CSRF-Token"] = csrf;

  const res = await fetch(apiUrl("/api/export"), {
    method: "POST",
    headers,
    credentials: "include",
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.detail) detail = typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
    } catch {
      // not JSON — keep the HTTP status message
    }
    if (res.status === 401) {
      _onUnauthorizedExport?.();
      detail = "Please sign in again to export";
    }
    throw new Error(detail);
  }

  const blob = await res.blob();
  triggerDownload(blob, buildFilename(res.headers.get("content-disposition"), payload.format));
}

/** Optional callback invoked when the export gets a 401 (used to trigger logout). */
let _onUnauthorizedExport: (() => void) | null = null;

export function setOnUnauthorizedExport(cb: () => void): void {
  _onUnauthorizedExport = cb;
}
