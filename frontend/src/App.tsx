import { useState, useEffect, useCallback, useRef, lazy, Suspense } from "react";
import { useStreaming } from "./useStreaming";
import { useAuth } from "./AuthContext";
import { AuthScreen } from "./AuthScreen";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { Settings } from "./Settings";
import { Sidebar } from "./Sidebar";
import { AppShell } from "./shell/AppShell";
import { Header } from "./shell/Header";
import { Inspector } from "./shell/Inspector";
import { ChatArea } from "./ChatArea";
import { ApprovalDialog } from "./components/ApprovalDialog";
import { CitationPopup } from "./CitationPopup";
import { DocumentPanel } from "./DocumentPanel";
import { MemoryPanel } from "./MemoryPanel";
import { TerminalPanel } from "./TerminalPanel";
import { MediaGenerationPanel } from "./MediaGenerationPanel";
// Lazy: the force-graph libs (~200 kB) load only when the panel is opened.
const KnowledgeGraphPanel = lazy(() =>
  import("./KnowledgeGraphPanel").then((m) => ({ default: m.KnowledgeGraphPanel }))
);
import { API } from "./api";
import type { Session, Message, Citation, Document, Memory, HealthStatus, SessionExport, UIChatMode } from "./types";
import "./App.css";
// ── App Component ─────────────────────────────────────────

function App() {
  // ═══════════════════════════════════════════════════════
  // ALL hooks must be declared BEFORE any conditional return
  // to satisfy React's Rules of Hooks.
  // ═══════════════════════════════════════════════════════

  // Phase 6C: Authentication gate
  const { user, isAuthenticated, isLoading: authLoading, logout } = useAuth();

  // Session state
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);

  // Chat state
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  // MP-3C3D3B — per-message response mode; "auto" is the default and omits
  // `mode` from the request entirely, preserving existing behavior.
  const [chatMode, setChatMode] = useState<UIChatMode>("auto");
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [retryTarget, setRetryTarget] = useState<{ message: string; errorDetail: string; image_url?: string } | null>(null);

  // OpenBot Autonomous Research (Phase 1) — the toggle only shows when the
  // user enabled the capability; autonomousMode routes sends to the task
  // queue instead of the chat stream. Polling is fully async: the composer
  // and the main chat NEVER block on a running research task.
  const [openbotEnabled, setOpenbotEnabled] = useState(false);
  const [autonomousMode, setAutonomousMode] = useState(false);
  const openbotPollsRef = useRef<
    Array<{ taskId: string; messageId: number; intervalId: number; pollCount: number }>
  >([]);

  // Citation popup state
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);

  // Per-message flags
  const [sourcesUsedIds, setSourcesUsedIds] = useState<Set<number>>(new Set());
  const [memoriesUsedIds, setMemoriesUsedIds] = useState<Set<number>>(new Set());

  // Document state
  const [documents, setDocuments] = useState<Document[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [showDocs, setShowDocs] = useState(false);

  // Model & system prompt state
  const [showSystemPromptEditor, setShowSystemPromptEditor] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);
  const [showMediaGeneration, setShowMediaGeneration] = useState(false);

  // Transient bottom-center notice — the project's existing toast idiom,
  // first introduced for the Shareable Agent Card. ONE state (and one
  // auto-dismiss effect) now drives every transient notice, including the
  // first-class YouTube ingest toast.
  const [appNotice, setAppNotice] = useState<{ kind: "info" | "warning"; message: string; timeoutMs: number } | null>(null);

  // Memory state
  const [memories, setMemories] = useState<Memory[]>([]);
  const [showMemories, setShowMemories] = useState(false);
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [memorySettingLoaded, setMemorySettingLoaded] = useState(false);
  const [togglePending, setTogglePending] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [addingMemory, setAddingMemory] = useState(false);
  const [newMemoryContent, setNewMemoryContent] = useState("");
  const [newMemoryCategory, setNewMemoryCategory] = useState("fact");
  const [editingMemoryId, setEditingMemoryId] = useState<number | null>(null);
  const [editMemoryContent, setEditMemoryContent] = useState("");
  const [editMemoryCategory, setEditMemoryCategory] = useState("fact");
  const [showClearConfirm, setShowClearConfirm] = useState(false);

  // Health state
  const [health, setHealth] = useState<HealthStatus | null>(null);

  // Streaming state
  const {
    isStreaming, streamedContent, startStream, cancelStream,
    durableRunId, phase: durablePhase, cancelDurableRun, resumeAfterApproval,
  } = useStreaming();
  const [generationStopped, setGenerationStopped] = useState(false);
  const [pendingToolApproval, setPendingToolApproval] = useState<{ tool: string; args: any; server?: string } | null>(null);
  // MP-3C3C — durable MCP approval (redacted; detail comes from the approval API)
  const [durableApproval, setDurableApproval] = useState<{ approvalId: string; toolId?: string } | null>(null);
  const durableRunTerminalRef = useRef(false);
  const [planSteps, setPlanSteps] = useState<Array<Record<string, unknown>>>([]);
  const [agentProgress, setAgentProgress] = useState<string | null>(null);
  // Real per-turn trace for the Thought accordion. Frames are not persisted,
  // so this resets on every send and is empty after a reload.
  const [agentEvents, setAgentEvents] = useState<Array<{ agent: string; message: string; at: number }>>([]);
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null);

  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const sessionListRequestRef = useRef(0);

  // Auto-dismiss every transient notice. Centralised here so the Share Card
  // feedback and the YouTube ingest toast share one mechanism.
  useEffect(() => {
    if (!appNotice) return;
    const t = setTimeout(() => setAppNotice(null), appNotice.timeoutMs);
    return () => clearTimeout(t);
  }, [appNotice]);

  // ── Health polling ─────────────────────────────────────

  const checkHealth = useCallback(async () => {
    try { setHealth(await API.getHealth()); } catch { setHealth(null); }
  }, []);

  useEffect(() => {
    checkHealth();
    const id = setInterval(checkHealth, 15_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  // ── Session CRUD ───────────────────────────────────────

  const loadSessions = useCallback(async () => {
    const requestId = ++sessionListRequestRef.current;
    setLoadingSessions(true);
    try {
      const data = await API.listSessions();
      if (requestId !== sessionListRequestRef.current) return;
      setSessions(data);
      // Stale-session recovery: if the selected session no longer exists in
      // the fetched list, clear it so the UI never points at a dead session.
      setActiveSessionId((prev) => {
        if (prev !== null && !data.some((s) => s.id === prev)) {
          return null;
        }
        return prev;
      });
    } catch { /* ignore */ }
    finally {
      if (requestId === sessionListRequestRef.current) setLoadingSessions(false);
    }
  }, []);

  // Reload sessions whenever auth state changes (login/logout). On logout,
  // clear account-specific state so another account never inherits it.
  useEffect(() => {
    sessionListRequestRef.current += 1;
    // D6C-J4: discard any in-flight message load (a logout must never be followed
    // by a stale state mutation) and re-arm initial hydration so a later login
    // hydrates again.
    messagesRequestRef.current += 1;
    hydratedSessionRef.current = null;
    setSessions([]);
    setActiveSessionId(null);
    setMessages([]);
    setDocuments([]);
    setChatError(null);
    setSourcesUsedIds(new Set());
    setMemoriesUsedIds(new Set());
    // OpenBot polls belong to a dead account once logged out — stop them.
    openbotPollsRef.current.forEach((p) => window.clearInterval(p.intervalId));
    openbotPollsRef.current = [];
    if (isAuthenticated) {
      loadSessions();
    } else {
      setLoadingSessions(false);
    }
  }, [isAuthenticated, user?.id, loadSessions]);

  const handleCreateSession = async () => {
    try {
      const session = await API.createSession();
      setSessions((prev) => [session, ...prev]);
      setActiveSessionId(session.id);
      setMessages([]);
      setChatError(null);
      setDocuments([]);
      setSourcesUsedIds(new Set());
      setMemoriesUsedIds(new Set());
    } catch { /* ignore */ }
  };

  const handleRenameSession = async (id: number, title: string) => {
    try {
      const updated = await API.updateSession(id, title);
      setSessions((prev) => prev.map((s) => (s.id === id ? updated : s)));
    } catch { /* ignore */ }
  };

  const handleDeleteSession = async (id: number) => {
    try {
      await API.deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (activeSessionId === id) { setActiveSessionId(null); setMessages([]); }
    } catch { /* ignore */ }
  };

  // ── Session export / import (F5) ───────────────────────

  const handleExportSession = async (id: number) => {
    try {
      const data = await API.exportSession(id);
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${(data.thunder_ai_export.session.title || "session").replace(/[^\w\- ]+/g, "_").replace(/ +/g, "_")}.agent.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setChatError(err instanceof Error ? err.message : "Export failed");
    }
  };

  const handleImportSession = async (file: File) => {
    try {
      const payload: SessionExport = JSON.parse(await file.text());
      if (!payload.thunder_ai_export || !payload.thunder_ai_export.session) {
        throw new Error("Not a valid Thunder AI agent file");
      }
      const result = await API.importSession(payload);
      await loadSessions();
      setActiveSessionId(result.session_id);
      setMessages([]);
      setChatError(null);
    } catch (err) {
      setChatError(err instanceof Error ? err.message : "Import failed");
    }
  };

  // ── Shareable Agent Card (F6) ─────────────────────────

  const handleShareSession = async (id: number) => {
    try {
      const result = await API.createShare(id);
      const url = result.share_url || `${window.location.origin}/share/agents/${result.share_id}`;
      try {
        await navigator.clipboard.writeText(url);
        setAppNotice({ kind: "info", message: "Share link copied to clipboard", timeoutMs: 3500 });
      } catch {
        setAppNotice({ kind: "info", message: "Share link created", timeoutMs: 3500 });
      }
      setChatError(null);
    } catch (err) {
      setChatError(err instanceof Error ? err.message : "Share failed");
    }
  };

  // ── Document loading ───────────────────────────────────

  const loadDocuments = useCallback(async (sessionId: number) => {
    try { setDocuments(await API.listDocuments(sessionId)); } catch { /* ignore */ }
  }, []);

  // ── Memory loading ─────────────────────────────────────

  const loadMemories = useCallback(async () => {
    try { setMemories(await API.listMemories()); } catch { /* ignore */ }
  }, []);

  // ── Load memory setting from backend on mount ─────────
  useEffect(() => {
    API.getMemorySetting()
      .then((r) => { setMemoryEnabled(r.enabled); })
      .catch(() => { /* backend might not be ready yet */ })
      .finally(() => { setMemorySettingLoaded(true); });
  }, []);

  // ── OpenBot capability lookup ──────────────────────────
  // Fetched on login and again when the Settings dialog closes, so a toggle
  // made in Settings > Skills is reflected in the composer without a reload.
  useEffect(() => {
    if (!isAuthenticated) { setOpenbotEnabled(false); return; }
    let cancelled = false;
    API.getCapabilities()
      .then((caps) => {
        if (!cancelled) {
          setOpenbotEnabled(
            caps.some((c) => c.id === "openbot_research" && c.enabled)
          );
        }
      })
      .catch(() => { if (!cancelled) setOpenbotEnabled(false); });
    return () => { cancelled = true; };
  }, [isAuthenticated, user?.id, showSettings]);

  // Stop every OpenBot poll when the component unmounts (no leaked timers).
  useEffect(() => () => {
    openbotPollsRef.current.forEach((p) => window.clearInterval(p.intervalId));
    openbotPollsRef.current = [];
  }, []);

  // ── Message loading ────────────────────────────────────

  // Tracks session IDs already recovered-from-404 so we refresh the list
  // exactly once (no infinite retry loop) when a session disappears.
  const recovered404Ref = useRef<Set<number>>(new Set());

  // D6C-J4: newest-wins guard for message loads. Mirrors the
  // `sessionListRequestRef` idiom in `loadSessions`, so a slow response can never
  // overwrite a newer selection (for example the initial hydration landing after
  // the user has already clicked a different session).
  const messagesRequestRef = useRef(0);

  // D6C-J4: the session id whose messages the automatic initial-selection path has
  // already hydrated. Keeps StrictMode's double-invoked mount effect, and any
  // unrelated re-run, from issuing a second identical request.
  const hydratedSessionRef = useRef<number | null>(null);

  const loadMessages = useCallback(async (sessionId: number) => {
    const requestId = ++messagesRequestRef.current;
    try {
      const data = await API.listMessages(sessionId);
      if (requestId !== messagesRequestRef.current) return; // a newer load won
      setMessages(data);
      setChatError(null);
      recovered404Ref.current.delete(sessionId);
    } catch (err) {
      if (requestId !== messagesRequestRef.current) return; // stale failure
      const msg = err instanceof Error ? err.message : "";
      if (/not found/i.test(msg)) {
        // Session is no longer available (deleted or not owned).
        setChatError("Session is no longer available");
        setActiveSessionId(null);
        setMessages([]);
        // Refresh the list once; guard against an infinite retry loop.
        if (!recovered404Ref.current.has(sessionId)) {
          recovered404Ref.current.add(sessionId);
          loadSessions();
        }
      } else {
        setChatError("Could not load messages");
      }
    }
  }, [loadSessions]);

  // Ensure the selected session is always present in the latest list; if the
  // user had nothing selected, select the first valid session when appropriate.
  //
  // D6C-J4: selecting is not enough - the selection must also be HYDRATED. Without
  // this, a page reload restored the session but rendered an empty conversation
  // ("0 messages") even though the messages were persisted and the API returned
  // them; the user had to click the session row to see them. `loadMessages` is the
  // same path `handleSelectSession` uses. The `hydratedSessionRef` guard keeps
  // StrictMode's double-invoked mount effect from issuing a second identical
  // request, and this branch only runs while `activeSessionId === null`, so an
  // already-active session is never replaced.
  useEffect(() => {
    if (activeSessionId === null && sessions.length > 0 && isAuthenticated) {
      const firstId = sessions[0].id;
      setActiveSessionId(firstId);
      if (hydratedSessionRef.current !== firstId) {
        hydratedSessionRef.current = firstId;
        loadMessages(firstId);
      }
    }
  }, [activeSessionId, sessions, isAuthenticated, loadMessages]);

  const handleSelectSession = (id: number) => {
    setActiveSessionId(id);
    setChatError(null);
    loadMessages(id);
    loadDocuments(id);
    setSourcesUsedIds(new Set());
    setMemoriesUsedIds(new Set());
  };

  const handleModelChange = (sessionId: number, model: string | null) => {
    // Keep the sessions list in sync so the saved model survives
    // reopens, without triggering a full reload that could reset it.
    setSessions((prev) =>
      prev.map((s) => (s.id === sessionId ? { ...s, model } : s))
    );
  };

  // ── Document upload ────────────────────────────────────

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !activeSessionId) return;
    setUploading(true);
    setUploadError(null);
    try {
      await API.uploadDocument(activeSessionId, file);
      loadDocuments(activeSessionId);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleDocumentUpload = async (file: File) => {
    if (!file || !activeSessionId) return;
    setUploading(true);
    setUploadError(null);
    try {
      await API.uploadDocument(activeSessionId, file);
      loadDocuments(activeSessionId);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleDeleteDocument = async (docId: number) => {
    try {
      await API.deleteDocument(docId);
      setDocuments((prev) => prev.filter((d) => d.id !== docId));
    } catch { /* ignore */ }
  };

  useEffect(() => {
    if (!activeSessionId) return;
    const hasProcessing = documents.some((d) => d.status === "processing");
    if (!hasProcessing) return;
    const id = setInterval(() => loadDocuments(activeSessionId), 2000);
    return () => clearInterval(id);
  }, [activeSessionId, documents]);

  // ── Memory CRUD ────────────────────────────────────────

  const handleToggleMemory = async () => {
    const newValue = !memoryEnabled;
    setTogglePending(true);
    try {
      const result = await API.setMemorySetting(newValue);
      setMemoryEnabled(result.enabled);
      if (!result.enabled) setMemoriesUsedIds(new Set());
    } catch {
      setMemoryError("Failed to update memory setting — toggle was not saved");
    } finally {
      setTogglePending(false);
    }
  };

  const handleAddMemory = async () => {
    const text = newMemoryContent.trim();
    if (!text) return;
    try {
      await API.createMemory(text, newMemoryCategory);
      setNewMemoryContent("");
      setAddingMemory(false);
      loadMemories();
      setMemoryError(null);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "Failed to add memory");
    }
  };

  const handleEditMemory = async (id: number) => {
    const text = editMemoryContent.trim();
    if (!text) return;
    try {
      await API.updateMemory(id, text, editMemoryCategory);
      setEditingMemoryId(null);
      loadMemories();
      setMemoryError(null);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "Failed to update memory");
    }
  };

  const handleDeleteMemory = async (id: number) => {
    try {
      await API.deleteMemory(id);
      setMemories((prev) => prev.filter((m) => m.id !== id));
    } catch { /* ignore */ }
  };

  const handleClearAllMemories = async () => {
    try {
      await API.clearAllMemories();
      setMemories([]);
      setShowClearConfirm(false);
    } catch { /* ignore */ }
  };

  // ── Send message (streaming) ───────────────────────────

  // OpenBot Autonomous Research (Phase 1). Routes the text to the task
  // queue and shows "Research in progress…" as an ephemeral assistant
  // message that the 2s poll loop later replaces with the final summary.
  // Strictly async: this never touches the streaming hook, so the main
  // chat (including sends of OTHER messages) stays interactive throughout.
  const startOpenbotResearch = useCallback(async (sessionId: number, text: string) => {
    const tempUserMsg: Message = {
      id: -Date.now(),
      session_id: sessionId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    setInput("");
    setChatError(null);

    let taskId: string;
    try {
      const res = await API.startOpenbotResearch(text);
      taskId = res.task_id;
    } catch (err) {
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
      setChatError(err instanceof Error ? err.message : "Failed to start the research task");
      return;
    }

    const loadingId = -(Date.now()) - 2;
    const loadingMsg: Message = {
      id: loadingId,
      session_id: sessionId,
      role: "assistant",
      content: "🤖 Research in progress…",
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, loadingMsg]);

    const finishPoll = (entry: { taskId: string; messageId: number; intervalId: number }) => {
      window.clearInterval(entry.intervalId);
      openbotPollsRef.current = openbotPollsRef.current.filter((p) => p !== entry);
    };
    const replaceLoading = (updater: (content: string) => string) => {
      setMessages((prev) =>
        prev.map((m) => (m.id === loadingId ? { ...m, content: updater(m.content) } : m))
      );
    };

    const entry = { taskId, messageId: loadingId, intervalId: 0, pollCount: 0 };
    entry.intervalId = window.setInterval(async () => {
      entry.pollCount += 1;
      // ~2 min cap (60 polls × 2s) so a stuck task cannot poll forever.
      if (entry.pollCount > 60) {
        finishPoll(entry);
        replaceLoading(() => "⚠️ Research timed out. The task may still finish in the background; please try again later.");
        return;
      }
      try {
        const task = await API.getOpenbotTask(taskId);
        if (task.status === "completed") {
          finishPoll(entry);
          replaceLoading(() => task.result_text || "Research finished without a summary.");
        } else if (task.status === "failed") {
          finishPoll(entry);
          replaceLoading(() => "⚠️ Autonomous research failed. Try rephrasing your query.");
        }
        // pending / running: keep polling silently
      } catch {
        // 404/403/5xx — a lost task is surfaced in the bubble, not as a fatal
        // chat error (the main chat must keep working).
        finishPoll(entry);
        replaceLoading(() => "⚠️ Lost track of the research task. Please try again.");
      }
    }, 2000);
    openbotPollsRef.current.push(entry);
  }, []);

  const handleSend = (imageUrl?: string, overrideText?: string) => {
    const text = (overrideText ?? input).trim();
    if (!text && !imageUrl) return;
    if (!activeSessionId || isStreaming || togglePending) return;

    // OpenBot routing (Phase 1): when Autonomous Mode is ON and the
    // capability is enabled, sends become asynchronous research tasks
    // instead of standard chat turns. Image attachments are not part of
    // the OpenBot contract yet — text-only.
    if (autonomousMode && openbotEnabled && text && !imageUrl) {
      void startOpenbotResearch(activeSessionId, text);
      return;
    }
    const originalText = text;

    setSending(true);
    setChatError(null);
    setRetryTarget(null);

    const tempUserMsg: Message = {
      id: -Date.now(),
      session_id: activeSessionId,
      role: "user",
      content: text,
      image_url: imageUrl || undefined,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    if (!overrideText) setInput("");

    // MP-3C3D3B: snapshot the currently-selected mode ONCE, synchronously,
    // at send time. "auto" resolves to `undefined` so the request omits
    // `mode` entirely; useStreaming's `submissionRef` then freezes this
    // value for the whole logical send, including every reconnect/resume.
    const modeSnapshot = chatMode === "auto" ? undefined : chatMode;

    durableRunTerminalRef.current = false;
    startStream(activeSessionId, text, imageUrl, {
      onStart: () => {
        setGenerationStopped(false); setPendingToolApproval(null);
        setDurableApproval(null); setPlanSteps([]);
        setAgentProgress(null);
        setAgentEvents([]); setTurnStartedAt(Date.now());
      },
      onToken: () => {},
      onPlan: (steps) => setPlanSteps(steps),
      onAgentProgress: setAgentProgress,
      onAgentStatus: (event) =>
        setAgentEvents((prev) => [
          ...prev,
          { agent: event.agent, message: event.message, at: Date.now() },
        ]),
      onYoutubeIngest: (d) => {
        // First-class ingest UX: info on success, warning otherwise, with the
        // server's exact bounded notice (fixed template + public title).
        // Auto-dismiss is handled by the shared appNotice effect.
        setAppNotice({
          kind: d.status === "success" ? "info" : "warning",
          message: d.notice,
          timeoutMs: d.status === "success" ? 4500 : 6000,
        });
      },
      onRunId: () => {},
      onApprovalRequired: (d) => {
        // MP-3C3C durable, redacted — open the informative approval dialog
        if (d.approvalId) setDurableApproval({ approvalId: d.approvalId, toolId: d.toolId });
      },
      onComplete: (result) => {
        setAgentProgress(null);
        durableRunTerminalRef.current = true;
        setGenerationStopped(false);
        setPendingToolApproval(null);
        setDurableApproval(null);
        if (result.sourcesUsed && result.messageId) {
          setSourcesUsedIds((prev) => new Set(prev).add(result.messageId));
        }
        if (result.memoriesUsed && result.messageId) {
          setMemoriesUsedIds((prev) => new Set(prev).add(result.messageId));
        }
        loadMessages(activeSessionId);
        loadMemories();
        setSending(false);
      },
      onError: (error) => {
        setAgentProgress(null);
        durableRunTerminalRef.current = true;
        setChatError(error.detail || "Failed to send message");
        setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
        // do not offer a one-click retry for transient durable-availability errors
        if (error.code !== "DURABLE_UNAVAILABLE" && error.code !== "RATE_LIMITED") {
          setRetryTarget({ message: originalText, errorDetail: error.detail || "Failed to send message", image_url: imageUrl });
        }
        setSending(false);
        setGenerationStopped(false);
        setPendingToolApproval(null);
        setDurableApproval(null);
      },
      onCancelled: () => {
        setAgentProgress(null);
        durableRunTerminalRef.current = true;
        setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id));
        setSending(false);
        setGenerationStopped(true);
        setPendingToolApproval(null);
        setDurableApproval(null);
        setTimeout(() => setGenerationStopped(false), 3000);
      },
      onToolApproval: (data) => {
        setPendingToolApproval(data);
      },
    }, modeSnapshot);
  };

  // Local Vision (ZERO API cost) — describe an attached image with the local
  // Ollama model and surface the result via the shared transient notice.
  // Fail-closed: a vision failure NEVER blocks attach/send.
  const handleImageAttached = useCallback(async (file: File) => {
    try {
      const res = await API.describeImage(file);
      if (res && res.status === "ok" && res.description) {
        const text = res.description.length > 280
          ? res.description.slice(0, 280).trimEnd() + "\u2026"
          : res.description;
        setAppNotice({ kind: "info", message: text, timeoutMs: 8000 });
      } else {
        setAppNotice({
          kind: "warning",
          message: (res && res.notice)
            || "I could not analyze the image. The chat will continue normally.",
          timeoutMs: 6000,
        });
      }
    } catch {
      setAppNotice({
        kind: "warning",
        message: "I could not analyze the image. The chat will continue normally.",
        timeoutMs: 6000,
      });
    }
  }, []);

  const handleRetry = useCallback(() => {
    if (!retryTarget || !activeSessionId || isStreaming) return;
    handleSend(retryTarget.image_url, retryTarget.message);
  }, [retryTarget, activeSessionId, isStreaming]);

  const handleToolApprove = useCallback(() => {
    if (!pendingToolApproval) return;
    setPendingToolApproval(null);
    // Send approval as next message - backend interprets as tool approval
    handleSend(undefined, "yes");
  }, [pendingToolApproval]);

  const handleToolReject = useCallback(() => {
    if (!pendingToolApproval) return;
    setPendingToolApproval(null);
    handleSend(undefined, "no");
  }, [pendingToolApproval]);

  // MP-3C3C — durable approval: the dialog already called the approve/deny API;
  // here we just keep/reconnect the stream to receive the continuation/denial.
  const handleDurableApproved = useCallback(() => { resumeAfterApproval(); }, [resumeAfterApproval]);
  const handleDurableDenied = useCallback(() => { resumeAfterApproval(); }, [resumeAfterApproval]);
  const handleDurableApprovalDismiss = useCallback(() => { setDurableApproval(null); }, []);

  // ═══════════════════════════════════════════════════════
  // Auth gate — after all hooks, before derived state & render
  // ═══════════════════════════════════════════════════════

  if (authLoading) {
    return (
      <div className="auth-loading-screen">
        <div className="auth-loading-spinner" />
        <p>Restoring session…</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <AuthScreen />;
  }

  // ── Derived state ──────────────────────────────────────

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;
  const docCount = documents.length;
  const readyDocs = documents.filter((d) => d.status === "ready").length;

  // ── Render ─────────────────────────────────────────────

  return (
    <>
      <AppShell
        sidebarOpen={sidebarOpen}
        inspectorOpen={inspectorOpen}
        onCloseSidebar={() => setSidebarOpen(false)}
        onCloseInspector={() => setInspectorOpen(false)}
        header={
          <Header
            sidebarOpen={sidebarOpen}
            onToggleSidebar={() => setSidebarOpen((v) => !v)}
            onNewChat={handleCreateSession}
            inspectorOpen={inspectorOpen}
            onToggleInspector={() => setInspectorOpen((v) => !v)}
            sessionId={activeSessionId}
            currentModel={activeSession?.model ?? null}
            onModelChange={(model) => {
              if (activeSession) handleModelChange(activeSession.id, model);
            }}
            onShare={() => {
              if (activeSession) handleShareSession(activeSession.id);
            }}
            shareDisabled={!activeSession}
          />
        }
        inspector={
          <Inspector
            open={inspectorOpen}
            onClose={() => setInspectorOpen(false)}
            turnMeta={{ startedAt: turnStartedAt, agentEvents, planSteps }}
            sessionId={activeSessionId}
          />
        }
        sidebar={
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        loadingSessions={loadingSessions}
        health={health}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onDeleteSession={handleDeleteSession}
        onRenameSession={handleRenameSession}
        onExportSession={handleExportSession}
        onImportSession={handleImportSession}
        onShareSession={handleShareSession}
        onOpenSettings={() => setShowSettings(true)}
        onOpenGraph={() => setShowGraph(true)}
        onOpenTerminal={() => setShowTerminal(true)}
        onOpenMediaGeneration={() => setShowMediaGeneration(true)}
        onLogout={logout}
        userName={user?.username}
      />
        }
      >

      <ChatArea
        activeSession={activeSession}
        messages={messages}
        sourcesUsedIds={sourcesUsedIds}
        memoriesUsedIds={memoriesUsedIds}
        onCitationClick={(citation: Citation) => setSelectedCitation(citation)}
        input={input}
        onInputChange={setInput}
        onSend={(imageUrl) => handleSend(imageUrl)}
        onKeyDown={(e: React.KeyboardEvent) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
        }}
        inputRef={inputRef}
        chatMode={chatMode}
        onChatModeChange={setChatMode}
        isStreaming={isStreaming}
        streamedContent={streamedContent}
        generationStopped={generationStopped}
        onCancelStream={durableRunId ? cancelDurableRun : cancelStream}
        chatError={chatError}
        retryTarget={retryTarget}
        onRetry={handleRetry}
        sessionModel={activeSession?.model ?? null}
        onModelChange={(model) => {
          if (activeSession) handleModelChange(activeSession.id, model);
        }}
        onOpenSystemPrompt={() => setShowSystemPromptEditor(true)}
        showDocs={showDocs}
        docCount={docCount}
        onToggleDocs={() => setShowDocs(!showDocs)}
        memorySettingLoaded={memorySettingLoaded}
        memoryEnabled={memoryEnabled}
        togglePending={togglePending}
        memoriesLength={memories.length}
        showMemories={showMemories}
        onToggleMemory={handleToggleMemory}
        onToggleMemories={() => { setShowMemories(!showMemories); loadMemories(); }}
        documents={documents}
        uploading={uploading}
        uploadError={uploadError}
        onDocumentUpload={handleDocumentUpload}
        onDeleteDocument={handleDeleteDocument}
        onImageAttached={handleImageAttached}
        pendingToolApproval={pendingToolApproval}
        onApproveTool={handleToolApprove}
        onRejectTool={handleToolReject}
        planSteps={planSteps}
        turnMeta={{ startedAt: turnStartedAt, agentEvents, planSteps }}
        onOpenInspector={() => setInspectorOpen(true)}
        agentProgress={agentProgress}
        onPlanCancel={() => setPlanSteps([])}
        onExportError={(msg) => setChatError(msg)}
        showModelSelector={false}
        openbotEnabled={openbotEnabled}
        autonomousMode={autonomousMode}
        onAutonomousModeChange={setAutonomousMode}
      />
      </AppShell>

      {/* MP-3C3C — durable MCP approval dialog (only when a durable run parked) */}
      <ApprovalDialog
        approvalId={durableApproval?.approvalId ?? null}
        phase={durablePhase}
        runTerminal={durableRunTerminalRef.current}
        onApprove={handleDurableApproved}
        onDeny={handleDurableDenied}
        onDismiss={handleDurableApprovalDismiss}
      />

      {/* MP-3C3C — non-blocking durable-run status (reconnect / resume / cancel) */}
      {durableRunId && (durablePhase === "reconnecting" || durablePhase === "resuming" || durablePhase === "cancelling") && (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-4 left-1/2 z-40 -translate-x-1/2 rounded-full border bg-background px-4 py-2 text-sm shadow-lg"
        >
          {durablePhase === "reconnecting" && "Reconnecting to the response…"}
          {durablePhase === "resuming" && "Resuming…"}
          {durablePhase === "cancelling" && "Cancelling…"}
        </div>
      )}

      {/* Document panel */}
      {showDocs && activeSession && (
        <DocumentPanel
          documents={documents}
          uploading={uploading}
          uploadError={uploadError}
          readyDocs={readyDocs}
          onFileSelect={handleFileSelect}
          onDeleteDocument={handleDeleteDocument}
        />
      )}

      {/* Memory panel */}
      {showMemories && (
        <MemoryPanel
          memories={memories}
          memoryEnabled={memoryEnabled}
          memorySettingLoaded={memorySettingLoaded}
          memoryError={memoryError}
          togglePending={togglePending}
          addingMemory={addingMemory}
          newMemoryContent={newMemoryContent}
          newMemoryCategory={newMemoryCategory}
          editingMemoryId={editingMemoryId}
          editMemoryContent={editMemoryContent}
          editMemoryCategory={editMemoryCategory}
          showClearConfirm={showClearConfirm}
          onToggleMemory={handleToggleMemory}
          onSetAddingMemory={setAddingMemory}
          onSetNewMemoryContent={setNewMemoryContent}
          onSetNewMemoryCategory={setNewMemoryCategory}
          onAddMemory={handleAddMemory}
          onEditMemory={handleEditMemory}
          onSetEditingMemoryId={setEditingMemoryId}
          onSetEditMemoryContent={setEditMemoryContent}
          onSetEditMemoryCategory={setEditMemoryCategory}
          onDeleteMemory={handleDeleteMemory}
          onClearAllMemories={handleClearAllMemories}
          onSetShowClearConfirm={setShowClearConfirm}
        />
      )}

      {/* Citation popup */}
      {selectedCitation && (
        <CitationPopup citation={selectedCitation} onClose={() => setSelectedCitation(null)} />
      )}

      {/* Transient notice (Share Card feedback + YouTube ingest toast) */}
      {appNotice && (
        <div
          role="status"
          aria-live="polite"
          className={"fixed bottom-6 left-1/2 z-50 max-w-[92vw] -translate-x-1/2 rounded-full border px-4 py-2 text-sm shadow-lg backdrop-blur " +
            (appNotice.kind === "warning"
              ? "border-amber-500/40 bg-amber-500/10 text-amber-100"
              : "border-white/10 bg-background/90 text-foreground")}
        >
          {appNotice.message}
        </div>
      )}

      {/* System prompt editor modal */}
      {showSystemPromptEditor && activeSession && (
        <SystemPromptEditor sessionId={activeSession.id} onClose={() => setShowSystemPromptEditor(false)} />
      )}

      {/* Settings modal */}
      {/* Settings must be usable BEFORE a session exists (provider configuration
          lives here). `Settings` already accepts `activeSessionId: number | null`
          and `sessions`, so the modal is NOT gated on an active session. */}
      {showSettings && (
        <Settings
          onClose={() => setShowSettings(false)}
          sessions={sessions}
          activeSessionId={activeSessionId}
        />
      )}

      {/* Knowledge Graph overlay */}
      {showGraph && (
        <Suspense fallback={null}>
          <KnowledgeGraphPanel onClose={() => setShowGraph(false)} />
        </Suspense>
      )}

      {/* Terminal overlay */}
      {showTerminal && (
        <TerminalPanel onClose={() => setShowTerminal(false)} />
      )}

      {/* Media Generation overlay */}
      {showMediaGeneration && activeSession && (
        <MediaGenerationPanel
          onClose={() => setShowMediaGeneration(false)}
          onMediaGenerated={(type, url) => {
            setMessages((prev) => [
              ...prev,
              {
                id: -Date.now(),
                session_id: activeSession.id,
                role: "assistant",
                content: `Generated ${type}:`,
                image_url: url,
                created_at: new Date().toISOString(),
              },
            ]);
          }}
        />
      )}
    </>
  );
}

export default App;
