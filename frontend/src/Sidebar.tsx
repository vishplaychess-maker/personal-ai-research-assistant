/**
 * Sidebar - Minimal session list (Grok style).
 * No heavy borders, subtle hover states, clean typography.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import {
  Plus,
  Search,
  X,
  Pencil,
  Trash2,
  Download,
  Upload,
  Share2,
  LogOut,
  Sun,
  Moon,
  Settings as SettingsIcon,
  MessageSquare,
  Globe,
  Loader2,
  Network,
  SquareTerminal,
  Image,
  Video,
} from "lucide-react";
import { getAuthHeadersAsync } from "./auth";
import { apiUrl } from "./apiBase";
import { useTheme } from "./components/ThemeProvider";
import { Button } from "./components/ui/button";
import { ScrollArea } from "./components/ui/scroll-area";
import { cn } from "./lib/utils";
import { MoreHorizontal } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./components/ui/dropdown-menu";
import type { Session, HealthStatus, SearchResult } from "./types";

function formatDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 86_400_000)
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** Bucket a session into the reference's date groups (newest first). */
function sessionGroupLabel(iso: string | undefined | null): string {
  if (!iso) return "Earlier";
  const created = new Date(iso);
  if (Number.isNaN(created.getTime())) return "Earlier";
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfToday.getDate() - 1);
  if (created >= startOfToday) return "Today";
  if (created >= startOfYesterday) return "Yesterday";
  if (created >= new Date(startOfToday.getTime() - 6 * 86_400_000)) return "Previous 7 days";
  return "Earlier";
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
  onExportSession?: (id: number) => void;
  onImportSession?: (file: File) => void;
  onShareSession?: (id: number) => void;
  onLogout?: () => void;
  onOpenSettings?: () => void;
  onOpenGraph?: () => void;
  onOpenTerminal?: () => void;
  onOpenMediaGeneration?: () => void;
  /** Signed-in user's display name (from AuthContext). */
  userName?: string;
  // Built-in browser (V1) — Chat/Browser view toggle in the sidebar body.
  activeView?: "chat" | "browser";
  onActiveViewChange?: (view: "chat" | "browser") => void;
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
  onExportSession,
  onImportSession,
  onShareSession,
  onLogout,
  onOpenSettings,
  onOpenGraph,
  onOpenTerminal,
  onOpenMediaGeneration,
  userName,
  activeView = "chat",
  onActiveViewChange,
}: SidebarProps) {
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");

  const importInputRef = useRef<HTMLInputElement | null>(null);

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
      const res = await fetch(apiUrl(`/api/search?q=${encodeURIComponent(query.trim())}`), {
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
    <aside className="flex h-full w-64 shrink-0 flex-col bg-background border-r border-white/5">
      {/* Header - Minimal */}
      <header className="flex items-center justify-between border-b border-white/10 px-3 py-3">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-5 w-5 icon-secondary" />
          <span className="text-sm font-medium text-foreground">Thunder AI</span>
        </div>
        <Button
          size="icon"
          variant="ghost"
          onClick={onCreateSession}
          title="New session"
          className="h-8 w-8 rounded-xl transition-colors hover:bg-white/5"
        >
          <Plus className="h-4 w-4" />
        </Button>
        {onImportSession && (
          <Button
            size="icon"
            variant="ghost"
            onClick={() => importInputRef.current?.click()}
            title="Import agent"
            className="h-8 w-8 rounded-xl transition-colors hover:bg-white/5"
          >
            <Upload className="h-4 w-4" />
          </Button>
        )}
      </header>

      {/* Import hidden input */}
      {onImportSession && (
        <input
          ref={importInputRef}
          type="file"
          accept="application/json,.json,.agent.json"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onImportSession(file);
            e.target.value = "";
          }}
        />
      )}

      {/* Search - Clean */}
      <div className="px-3 py-2 border-b border-white/10">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 icon-muted" />
          <input
            className="h-9 w-full rounded-xl bg-white/5 border border-transparent px-9 py-0 text-sm outline-none placeholder:text-placeholder focus:border-primary/30 focus:bg-white/5 transition-all"
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
              className="absolute right-3 top-1/2 -translate-y-1/2 icon-muted transition-colors hover:icon-secondary"
              onClick={handleClearSearch}
              title="Clear search"
            >
              <X className="h-4 w-4" />
            </button>
          )}
          {isSearching && (
            <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin icon-muted" />
          )}
        </div>
      </div>

      {/* View toggle — Chat / Built-in Browser (V1) */}
      {onActiveViewChange && (
        <div className="flex gap-1 border-b border-white/10 px-2 py-2">
          {(
            [
              { id: "chat" as const, label: "Chat", icon: MessageSquare },
              { id: "browser" as const, label: "Browser", icon: Globe },
            ]
          ).map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => onActiveViewChange(id)}
              aria-pressed={activeView === id}
              title={label === "Browser" ? "Built-in browser" : "Back to chat"}
              data-testid={`sidebar-view-${id}`}
              className={cn(
                "flex flex-1 items-center justify-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-medium transition-all",
                activeView === id
                  ? "bg-primary/10 text-primary"
                  : "text-secondary hover:bg-white/5"
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" />
              {label}
            </button>
          ))}
        </div>
      )}

      {/* List */}
      <ScrollArea className="flex-1">
        <div className="p-2">
          {isSearchActive ? (
            <>
              {searchError && (
                <div className="mb-2 px-3 py-2 text-xs text-destructive">{searchError}</div>
              )}
              {isSearching && searchResults.length === 0 ? (
                <div className="px-3 py-6 text-center text-sm text-muted-foreground">Searching…</div>
              ) : !isSearching && searchResults.length === 0 ? (
                <div className="px-3 py-6 text-center text-sm text-muted-foreground">No results found</div>
              ) : (
                searchResults.map((result) => (
                  <div
                    key={result.message_id}
                    className={cn(
                      "mb-1 cursor-pointer rounded-lg px-3 py-2 transition-colors hover:bg-white/5",
                      result.session_id === activeSessionId && "bg-white/5"
                    )}
                    onClick={() => handleSearchResultClick(result)}
                  >
                    <div className="truncate text-sm font-medium text-foreground">{result.session_title}</div>
                    <div className="truncate text-xs text-secondary">{result.snippet}</div>
                    <div className="mt-1 text-[10px] text-muted">{result.role} · {formatDate(result.created_at)}</div>
                  </div>
                ))
              )}
            </>
          ) : loadingSessions ? (
            <div className="px-3 py-6 text-center text-sm text-secondary">Loading…</div>
          ) : sessions.length === 0 ? (
            <div className="px-3 py-8 text-center">
              <MessageSquare className="mx-auto mb-2 h-8 w-8 icon-muted" />
              <p className="text-sm text-secondary">No conversations yet</p>
              <p className="mt-1 text-xs text-muted">Click + to start a new conversation</p>
            </div>
          ) : (
            sessions.map((session, index) => {
              const isActive = session.id === activeSessionId;
              const groupLabel = sessionGroupLabel(session.created_at);
              const prevLabel =
                index > 0 ? sessionGroupLabel(sessions[index - 1].created_at) : "";
              return (
                <div key={session.id}>
                  {groupLabel !== prevLabel ? (
                    <div className="px-3 pb-1 pt-3 text-[11px] font-medium text-secondary">
                      {groupLabel}
                    </div>
                  ) : null}
                  <div
                    className={cn(
                    "group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-sm transition-colors duration-150",
                    isActive ? "bg-white/5 text-foreground" : "hover:bg-white/5"
                  )}
                  onClick={() => onSelectSession(session.id)}
                >
                  <div className={cn("flex-1 flex items-center gap-2 min-w-0", isActive ? "ml-1" : "ml-2")}>
                    <MessageSquare className={cn(
                      "h-4 w-4 shrink-0 transition-colors",
                      isActive ? "text-primary" : "icon-secondary"
                    )} />
                    {renamingId === session.id ? (
                      <input
                        className="h-7 w-full rounded border bg-background px-2 text-sm outline-none focus:border-primary focus:ring-1 focus:ring-primary"
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
                        <span className={cn("truncate font-medium", isActive ? "text-foreground" : "text-secondary")}>
                          {session.title}
                        </span>
                        <span className="shrink-0 text-[10px] text-muted">
                          {formatDate(session.updated_at)}
                        </span>
                      </>
                    )}
                  </div>
                  <div
                    className="hidden shrink-0 items-center gap-0.5 opacity-0 group-hover:opacity-100 group-hover:flex transition-opacity duration-150"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      className="h-7 w-7 rounded-lg icon-muted hover:icon-primary hover:bg-white/5 transition-all"
                      title="Rename"
                      onClick={() => handleRenameStart(session)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    {onShareSession && (
                      <button
                        className="h-7 w-7 rounded-lg icon-muted hover:text-primary hover:bg-white/5 transition-all"
                        title="Share agent card"
                        onClick={() => onShareSession(session.id)}
                      >
                        <Share2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                    {onExportSession && (
                      <button
                        className="h-7 w-7 rounded-lg icon-muted hover:icon-primary hover:bg-white/5 transition-all"
                        title="Export agent"
                        onClick={() => onExportSession(session.id)}
                      >
                        <Download className="h-3.5 w-3.5" />
                      </button>
                    )}
                    <button
                      className="h-7 w-7 rounded-lg icon-muted hover:text-destructive hover:bg-destructive/10 transition-all"
                      title="Delete"
                      onClick={() => onDeleteSession(session.id)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </ScrollArea>

      {/* Footer — compact single row (~52px). Everything else lives in the
          overflow menu so no action is hover-only or unreachable by keyboard. */}
      <footer className="border-t border-white/10 px-2 py-1.5">
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <div className="group flex h-9 items-center gap-2 rounded-lg px-1.5 transition-colors hover:bg-white/5 focus-within:bg-white/5">
            <button
              type="button"
              onClick={() => setMenuOpen(true)}
              aria-label="Account and actions"
              className="flex min-w-0 flex-1 items-center gap-2 rounded-md text-left"
            >
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white/10 text-[11px] font-medium text-foreground">
                {(userName ?? "?").slice(0, 2).toUpperCase()}
              </span>
              <span className="truncate text-[13px] font-medium text-foreground">
                {userName ?? "Account"}
              </span>
            </button>
            {onOpenSettings && (
              <button
                type="button"
                onClick={onOpenSettings}
                title="Settings"
                aria-label="Settings"
                className="rounded-md p-1.5 text-secondary opacity-60 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
              >
                <SettingsIcon className="h-4 w-4" />
              </button>
            )}
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                title="More actions"
                aria-label="More actions"
                className="rounded-md p-1.5 text-secondary opacity-60 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
              >
                <MoreHorizontal className="h-4 w-4" />
              </button>
            </DropdownMenuTrigger>
          </div>

          <DropdownMenuContent align="start" side="top" className="w-56">
            <DropdownMenuLabel className="flex items-center gap-1.5 text-[10px] font-normal text-secondary">
              <span className={cn("h-1.5 w-1.5 rounded-full", health?.backend === "ok" ? "bg-green-500" : "bg-destructive")} />
              API
              <span className={cn("h-1.5 w-1.5 rounded-full", health?.ollama === "ok" ? "bg-green-500" : "bg-destructive")} />
              LLM
              <span className={cn("h-1.5 w-1.5 rounded-full", health?.chromadb === "ok" ? "bg-green-500" : "bg-destructive")} />
              DB
              <span className="ml-auto font-mono">{healthOk === 3 ? "3/3" : `${healthOk}/3`}</span>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => toggleTheme()}>
              {theme === "paper" ? <Moon className="mr-2 h-3.5 w-3.5" /> : <Sun className="mr-2 h-3.5 w-3.5" />}
              {theme === "paper" ? "Dark theme" : "Paper theme"}
            </DropdownMenuItem>
            {onOpenGraph && <DropdownMenuItem onSelect={onOpenGraph}>Knowledge Graph</DropdownMenuItem>}
            {onOpenTerminal && <DropdownMenuItem onSelect={onOpenTerminal}>Terminal</DropdownMenuItem>}
            {onOpenMediaGeneration && <DropdownMenuItem onSelect={onOpenMediaGeneration}>Media Generation</DropdownMenuItem>}
            <DropdownMenuSeparator />
            {onLogout && (
              <DropdownMenuItem onSelect={onLogout} className="text-destructive">
                Sign out
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </footer>
    </aside>
  );
}

export default Sidebar;