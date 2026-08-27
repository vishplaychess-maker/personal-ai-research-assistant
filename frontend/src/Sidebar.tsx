/**
 * Sidebar - Session list with search, CRUD, health indicators, theme toggle.
 * White/Blue theme (Tailwind + shadcn/ui); active session highlighted in blue.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import {
  Plus,
  Search,
  X,
  Pencil,
  Trash2,
  LogOut,
  Sun,
  Moon,
  Settings as SettingsIcon,
  MessageSquare,
  Loader2,
} from "lucide-react";
import { getAuthHeadersAsync } from "./auth";
import { useTheme } from "./components/ThemeProvider";
import { Button } from "./components/ui/button";
import { ScrollArea } from "./components/ui/scroll-area";
import { cn } from "./lib/utils";
import type { Session, HealthStatus, SearchResult } from "./types";

function formatDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 86_400_000)
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

interface SidebarProps {
  sessions: Session[];
  activeSessionId: number | null;
  loadingSessions: boolean;
  health: HealthStatus | null;
  onSelectSession: (id: number) => void;
  onCreateSession: () => void;
  onDeleteSession: (id: number) => void;
  onRenameSession: (id: number, title: string) => void;
  onLogout?: () => void;
  onOpenSettings?: () => void;
}

export function Sidebar({
  sessions,
  activeSessionId,
  loadingSessions,
  health,
  onSelectSession,
  onCreateSession,
  onDeleteSession,
  onRenameSession,
  onLogout,
  onOpenSettings,
}: SidebarProps) {
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const searchAbortRef = useRef<AbortController | null>(null);

  const { theme, toggleTheme } = useTheme();

  const healthOk = health
    ? Object.values(health).filter((v) => v === "ok").length
    : 0;

  const handleRenameStart = (session: Session) => {
    setRenamingId(session.id);
    setRenameValue(session.title);
  };

  const handleRenameSubmit = async (id: number) => {
    const title = renameValue.trim() || "Untitled";
    await onRenameSession(id, title);
    setRenamingId(null);
  };

  const performSearch = useCallback(async (query: string) => {
    if (searchAbortRef.current) searchAbortRef.current.abort();
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
      const auth = await getAuthHeadersAsync();
      const res = await fetch(`/api/search?q=${encodeURIComponent(query.trim())}`, {
        headers: auth.Authorization ? { Authorization: auth.Authorization } : {},
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
      if (!controller.signal.aborted) {
        setSearchResults(data);
        setSearchError(null);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setSearchError(err instanceof Error ? err.message : "Search failed");
      setSearchResults([]);
    } finally {
      if (!controller.signal.aborted) setIsSearching(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => performSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery, performSearch]);

  useEffect(() => {
    return () => {
      if (searchAbortRef.current) searchAbortRef.current.abort();
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
    if (searchAbortRef.current) searchAbortRef.current.abort();
  };

  const isSearchActive = searchQuery.trim().length > 0;

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r bg-background">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-3 py-3">
        <span className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <MessageSquare className="h-4 w-4" />
          </span>
          Research
        </span>
        <Button size="icon" variant="ghost" onClick={onCreateSession} title="New session">
          <Plus className="h-4 w-4" />
        </Button>
      </div>

      {/* Search */}
      <div className="px-3 py-2">
        <div className="flex items-center gap-2 rounded-md border bg-muted/50 px-2 focus-within:ring-1 focus-within:ring-ring">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            className="h-9 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            type="text"
            placeholder="Search conversations"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") handleClearSearch();
            }}
          />
          {searchQuery && (
            <button
              className="text-muted-foreground transition-colors hover:text-foreground"
              onClick={handleClearSearch}
              title="Clear search"
            >
              <X className="h-4 w-4" />
            </button>
          )}
          {isSearching && (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
          )}
        </div>
      </div>

      {/* List */}
      <ScrollArea className="flex-1">
        <div className="p-2">
          {isSearchActive ? (
            <>
              {searchError && (
                <div className="px-3 py-2 text-xs text-destructive">{searchError}</div>
              )}
              {isSearching && searchResults.length === 0 ? (
                <div className="px-3 py-6 text-center text-sm text-muted-foreground">
                  Searching...
                </div>
              ) : !isSearching && searchResults.length === 0 ? (
                <div className="px-3 py-6 text-center text-sm text-muted-foreground">
                  No results found
                </div>
              ) : (
                searchResults.map((result) => (
                  <div
                    key={result.message_id}
                    className={cn(
                      "mb-1 cursor-pointer rounded-md px-3 py-2 transition-colors hover:bg-muted",
                      result.session_id === activeSessionId && "bg-muted"
                    )}
                    onClick={() => handleSearchResultClick(result)}
                  >
                    <div className="truncate text-sm font-medium text-foreground">
                      {result.session_title}
                    </div>
                    <div className="truncate text-xs text-muted-foreground">
                      {result.snippet}
                    </div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      {result.role} · {formatDate(result.created_at)}
                    </div>
                  </div>
                ))
              )}
            </>
          ) : loadingSessions ? (
            <div className="px-3 py-6 text-center text-sm text-muted-foreground">
              Loading...
            </div>
          ) : sessions.length === 0 ? (
            <div className="px-3 py-6 text-center text-sm text-muted-foreground">
              No sessions yet
            </div>
          ) : (
            sessions.map((session) => (
              <div
                key={session.id}
                className={cn(
                  "group mb-1 flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-sm transition-colors",
                  session.id === activeSessionId
                    ? "bg-primary text-primary-foreground"
                    : "hover:bg-muted"
                )}
                onClick={() => onSelectSession(session.id)}
              >
                <MessageSquare className="h-4 w-4 shrink-0 opacity-70" />
                {renamingId === session.id ? (
                  <input
                    className="h-7 w-full rounded border bg-background px-2 text-sm outline-none"
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
                    <span className="flex-1 truncate">{session.title}</span>
                    <span className="shrink-0 text-[10px] opacity-60">
                      {formatDate(session.updated_at)}
                    </span>
                    <div
                      className="hidden shrink-0 items-center gap-0.5 group-hover:flex"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        className="rounded p-1 opacity-70 transition-opacity hover:opacity-100"
                        title="Rename"
                        onClick={() => handleRenameStart(session)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                      <button
                        className="rounded p-1 opacity-70 transition-opacity hover:opacity-100"
                        title="Delete"
                        onClick={() => onDeleteSession(session.id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))
          )}
        </div>
      </ScrollArea>

      {/* Footer */}
      <div className="border-t px-3 py-2">
        <div className="mb-2 flex items-center gap-2 text-[11px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                health?.backend === "ok" ? "bg-green-500" : "bg-destructive"
              )}
            />
            API
          </span>
          <span className="flex items-center gap-1">
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                health?.ollama === "ok" ? "bg-green-500" : "bg-destructive"
              )}
            />
            LLM
          </span>
          <span className="ml-auto">
            {healthOk === 3 ? "All OK" : `${healthOk}/3`}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <Button
            size="icon"
            variant="ghost"
            onClick={toggleTheme}
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {theme === "dark" ? (
              <Sun className="h-4 w-4" />
            ) : (
              <Moon className="h-4 w-4" />
            )}
          </Button>
          {onOpenSettings && (
            <Button
              size="icon"
              variant="ghost"
              onClick={onOpenSettings}
              title="Settings"
            >
              <SettingsIcon className="h-4 w-4" />
            </Button>
          )}
          {onLogout && (
            <Button size="icon" variant="ghost" onClick={onLogout} title="Sign out">
              <LogOut className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>
    </aside>
  );
}

export default Sidebar;
