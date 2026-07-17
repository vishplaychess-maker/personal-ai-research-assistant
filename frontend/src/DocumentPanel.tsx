/**
 * DocumentPanel — Document upload and list panel.
 *
 * Extracted from App.tsx in Phase 5C.
 */

import { useRef } from "react";
import type { Document } from "./types";

// ── Helpers ───────────────────────────────────────────────

function formatFileSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── Types ─────────────────────────────────────────────────

interface DocumentPanelProps {
  documents: Document[];
  uploading: boolean;
  uploadError: string | null;
  readyDocs: number;
  onFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onDeleteDocument: (id: number) => void;
}

// ── Component ─────────────────────────────────────────────

export function DocumentPanel({
  documents,
  uploading,
  uploadError,
  readyDocs,
  onFileSelect,
  onDeleteDocument,
}: DocumentPanelProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleLocalFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    onFileSelect(e);
    // Reset the input value so the same file can be re-selected
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div className="doc-panel">
      <div className="doc-panel-header">
        <span className="doc-panel-title">📄 Documents</span>
        <span className="doc-panel-subtitle">
          {readyDocs} ready ·{" "}
          {documents.filter((d) => d.status === "processing").length}{" "}
          processing
        </span>
      </div>

      <div className="doc-upload-row">
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.txt"
          onChange={handleLocalFileSelect}
          style={{ display: "none" }}
        />
        <button
          className="doc-upload-btn"
          onClick={() => fileInputRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? "⏳ Uploading…" : "📤 Upload PDF/TXT"}
        </button>
        <span className="doc-upload-hint">Max 20 MB</span>
      </div>

      {uploadError && <div className="doc-error">{uploadError}</div>}

      <div className="doc-list">
        {documents.length === 0 ? (
          <div className="doc-empty">No documents uploaded yet</div>
        ) : (
          documents.map((doc) => (
            <div key={doc.id} className={`doc-item doc-${doc.status}`}>
              <div className="doc-info">
                <span className="doc-icon">
                  {doc.filename.endsWith(".pdf") ? "📕" : "📄"}
                </span>
                <div className="doc-details">
                  <span className="doc-name">{doc.filename}</span>
                  <span className="doc-meta">
                    {doc.status === "processing" && "⏳ Processing…"}
                    {doc.status === "ready" && `✅ ${doc.chunk_count} chunks`}
                    {doc.status === "failed" &&
                      `❌ Failed: ${doc.error_message || "Unknown error"}`}
                    {formatFileSize(doc.file_size) &&
                      ` · ${formatFileSize(doc.file_size)}`}
                  </span>
                </div>
              </div>
              <button
                className="doc-delete-btn"
                onClick={() => onDeleteDocument(doc.id)}
                title="Delete document"
                disabled={doc.status === "processing"}
              >
                ✕
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default DocumentPanel;
