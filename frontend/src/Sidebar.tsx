/**
 * Sidebar — Session list with search, CRUD, and health indicators.
 *
 * Phase 5C: Added conversation search bar with debounced input,
 * result display, and click-to-navigate.
 */

import { useState, useEffect, useRef, useCallback } from "react";
import type { Session, HealthStatus, SearchResult } from "./types";

// ── Helpers ───────────────────────────────────────────────

function formatDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 86_400_000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ── Types ─────────────────────────────────────────────────

interface SidebarProps {
  sessions: Session[];
  activeSessionId: number | null;
  loadingSessions: boolean;
  health: HealthStatus | null;
  onSelectSession: (id: number) => void;
  onCreateSession: () => void;
  onDeleteSession: (id: number) => void;
  onRenameSession: (id: number, title: string) => void;
}

// ── Component ─────────────────────────────────────────────

export function Sidebar({
  sessions,
  activeSessionId,
  loadingSessions,
  health,
  onSelectSession,
  onCreateSession,
  onDeleteSession,
  onRenameSession,
}: SidebarProps) {
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // Search state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const searchAbortRef = useRef<AbortController | null>(null);

  const healthOk = health
    ? Object.values(health).filter((v) => v === "ok").length
    : 0;

  // ── Rename handlers ─────────────────────────────────────

  const handleRenameStart = (session: Session) => {
    setRenamingId(session.id);
    setRenameValue(session.title);
  };

  const handleRenameSubmit = async (id: number) => {
    const title = renameValue.trim() || "Untitled";
    await onRenameSession(id, title);
    setRenamingId(null);
  };

  // ── Search ──────────────────────────────────────────────

  const performSearch = useCallback(async (query: string) => {
    // Abort any in-flight search request
    if (searchAbortRef.current) {
      searchAbortRef.current.abort();
    }

    if (!query.trim()) {
      setSearchResults([]);
      setIsSearching(false);
      setSearchError(null);
      return;
    }

    const controller = new AbortController();
    searchAbortRef.current = controller;
    setIsSearching(true);
    setSearchError(null);

    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(query.trim())}`, {
        signal: controller.signal,
      });

      if (!res.ok) {
        if (res.status === 400) {
          setSearchResults([]);
          setIsSearching(false);
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }

      const data: SearchResult[] = await res.json();
      // Only update if this request wasn't aborted
      if (!controller.signal.aborted) {
        setSearchResults(data);
        setSearchError(null);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return; // Ignore aborted requests
      }
      setSearchError(err instanceof Error ? err.message : "Search failed");
      setSearchResults([]);
    } finally {
      if (!controller.signal.aborted) {
        setIsSearching(false);
      }
    }
  }, []);

  // Debounced search: fire 300ms after the user stops typing
  useEffect(() => {
    const timer = setTimeout(() => {
      performSearch(searchQuery);
    }, 300);

    return () => clearTimeout(timer);
  }, [searchQuery, performSearch]);

  // Cleanup search on unmount
  useEffect(() => {
    return () => {
      if (searchAbortRef.current) {
        searchAbortRef.current.abort();
      }
    };
  }, []);

  const handleSearchResultClick = (result: SearchResult) => {
    onSelectSession(result.session_id);
    setSearchQuery("");
    setSearchResults([]);
  };

  const handleClearSearch = () => {
    setSearchQuery("");
    setSearchResults([]);
    setIsSearching(false);
    setSearchError(null);
    if (searchAbortRef.current) {
      searchAbortRef.current.abort();
    }
  };

  const isSearchActive = searchQuery.trim().length > 0;

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="sidebar-logo">🧠</span>
        <span className="sidebar-title">Research Sessions</span>
        <button className="new-session-btn" onClick={onCreateSession}>
          ✚ New
        </button>
      </div>

      {/* Search bar */}
      <div className="sidebar-search">
        <span className="sidebar-search-icon">🔍</span>
        <input
          className="sidebar-search-input"
          type="text"
          name="search"
          placeholder="Search conversations…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") handleClearSearch();
          }}
        />
        {searchQuery && (
          <button className="sidebar-search-clear" onClick={handleClearSearch} title="Clear search">
            ✕
          </button>
        )}
        {isSearching && <span className="sidebar-search-spinner">⟳</span>}
      </div>

      <div className="sidebar-list">
        {isSearchActive ? (
          // ── Search results view ──────────────────────────
          <>
            {searchError && (
              <div className="sidebar-search-error">{searchError}</div>
            )}
            {isSearching && searchResults.length === 0 ? (
              <div className="sidebar-empty">Searching…</div>
            ) : !isSearching && searchResults.length === 0 ? (
              <div className="sidebar-empty">
                <div style={{ marginBottom: "0.5rem", fontSize: "1.2rem" }}>🔍</div>
                No results found
              </div>
            ) : (
              searchResults.map((result) => (
                <div
                  key={result.message_id}
                  className={`sidebar-item ${result.session_id === activeSessionId ? "active" : ""}`}
                  onClick={() => handleSearchResultClick(result)}
                >
                  <span className="sidebar-item-icon">
                    {result.role === "user" ? "🙋" : "🤖"}
                  </span>
                  <div className="sidebar-search-result">
                    <span className="sidebar-item-title">{result.session_title}</span>
                    <span className="sidebar-search-snippet">{result.snippet}</span>
                    <span className="sidebar-search-meta">
                      {result.role} · {formatDate(result.created_at)}
                    </span>
                  </div>
                </div>
              ))
            )}
          </>
        ) : (
          // ── Normal session list view ─────────────────────
          <>
            {loadingSessions ? (
              <div className="sidebar-empty">Loading…</div>
            ) : sessions.length === 0 ? (
              <div className="sidebar-empty">
                <div style={{ marginBottom: "0.5rem", fontSize: "1.2rem" }}>📂</div>
                No sessions yet
              </div>
            ) : (
              sessions.map((session) => (
                <div
                  key={session.id}
                  className={`sidebar-item ${session.id === activeSessionId ? "active" : ""}`}
                  onClick={() => onSelectSession(session.id)}
                >
                  <span className="sidebar-item-icon">💬</span>
                  {renamingId === session.id ? (
                    <input
                      className="rename-input"
                      name="rename"
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onBlur={() => handleRenameSubmit(session.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleRenameSubmit(session.id);
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                      autoFocus
                      onClick={(e) => e.stopPropagation()}
                    />
                  ) : (
                    <>
                      <span className="sidebar-item-title">{session.title}</span>
                      <span
                        style={{
                          fontSize: "0.65rem",
                          color: "var(--text-secondary)",
                          flexShrink: 0,
                        }}
                      >
                        {formatDate(session.updated_at)}
                      </span>
                      <div
                        className="sidebar-item-actions"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <button
                          className="sidebar-action-btn"
                          title="Rename"
                          onClick={() => handleRenameStart(session)}
                        >
                          ✎
                        </button>
                        <button
                          className="sidebar-action-btn delete"
                          title="Delete"
                          onClick={() => onDeleteSession(session.id)}
                        >
                          ✕
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))
            )}
          </>
        )}
      </div>

      <div className="sidebar-footer">
        <span
          className={`health-indicator ${health?.backend === "ok" ? "ok" : "unavailable"}`}
          title={`Backend: ${health?.backend ?? "?"}`}
        >
          <span className="health-dot" />
          API
        </span>
        <span
          className={`health-indicator ${health?.ollama === "ok" ? "ok" : "unavailable"}`}
          title={`Ollama: ${health?.ollama ?? "?"}`}
        >
          <span className="health-dot" />
          LLM
        </span>
        <span style={{ marginLeft: "auto", fontSize: "0.65rem", color: "var(--text-secondary)" }}>
          {healthOk === 3 ? "All OK" : `${healthOk}/3`}
        </span>
      </div>
    </aside>
  );
}

export default Sidebar;
