/**
 * CitationPopup — Overlay showing citation source details.
 *
 * Extracted from App.tsx in Phase 5C.
 */

import type { Citation } from "./types";

interface CitationPopupProps {
  citation: Citation;
  onClose: () => void;
}

export function CitationPopup({ citation, onClose }: CitationPopupProps) {
  return (
    <div className="citation-overlay" onClick={onClose}>
      <div className="citation-popup" onClick={(e) => e.stopPropagation()}>
        <div className="citation-popup-header">
          <span className="citation-popup-marker">{citation.marker}</span>
          <span className="citation-popup-file">{citation.filename}</span>
          <button className="citation-popup-close" onClick={onClose}>
            ✕
          </button>
        </div>
        {citation.page_number && (
          <div className="citation-popup-meta">Page {citation.page_number}</div>
        )}
        <div className="citation-popup-snippet">&quot;{citation.snippet}&quot;</div>
      </div>
    </div>
  );
}

export default CitationPopup;
