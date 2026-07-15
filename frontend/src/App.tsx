import { useState, useEffect, useCallback } from "react";
import "./App.css";

interface HealthStatus {
  backend: string;
  chromadb: string;
  ollama: string;
}

const SERVICES = [
  { key: "backend", icon: "⚙️", name: "Backend (FastAPI)" },
  { key: "chromadb", icon: "🗄️", name: "ChromaDB (Vector DB)" },
  { key: "ollama", icon: "🤖", name: "Ollama (Local AI)" },
] as const;

function App() {
  const [status, setStatus] = useState<HealthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const checkHealth = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/health");
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      const data: HealthStatus = await res.json();
      setStatus(data);
      setLastChecked(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connection failed");
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const id = setInterval(checkHealth, 15_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  const okCount = status
    ? Object.values(status).filter((v) => v === "ok").length
    : 0;
  const totalCount = SERVICES.length;

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-icon">🧠</div>
        <h1>Personal AI Research Assistant</h1>
        <p className="subtitle">Phase 1 — Service Health Monitor</p>
      </header>

      <main className="main-content">
        {/* Summary bar */}
        <div className="summary-bar">
          {status && !error && (
            <span className="summary-text">
              <span className="summary-ok">{okCount}</span>
              <span className="summary-sep">/</span>
              <span>{totalCount}</span> services online
            </span>
          )}
          <span
            className={`connection-badge ${
              error ? "error" : status ? "connected" : ""
            }`}
          >
            <span className="badge-dot" />
            {loading && !status
              ? "Connecting…"
              : error
                ? "Disconnected"
                : "Connected"}
          </span>
          {lastChecked && (
            <span className="last-checked">
              Updated {lastChecked.toLocaleTimeString()}
            </span>
          )}
        </div>

        {/* Error banner */}
        {error && (
          <div className="error-banner">
            <span className="error-icon">⚠️</span>
            <span>Backend unreachable: {error}</span>
            <button
              className="retry-btn"
              onClick={checkHealth}
              disabled={loading}
            >
              Retry
            </button>
          </div>
        )}

        {/* Health cards */}
        <div className="cards-grid">
          {SERVICES.map((svc) => (
            <div
              key={svc.key}
              className={`health-card ${
                loading
                  ? "loading"
                  : status?.[svc.key] === "ok"
                    ? "healthy"
                    : "unhealthy"
              }`}
            >
              <div className="card-glow" />
              <div className="card-icon">{svc.icon}</div>
              <div className="card-body">
                <h3 className="card-title">{svc.name}</h3>
                <span
                  className={`status-badge ${
                    loading
                      ? "badge-pending"
                      : status?.[svc.key] === "ok"
                        ? "badge-ok"
                        : "badge-down"
                  }`}
                >
                  {loading
                    ? "Checking…"
                    : status?.[svc.key] === "ok"
                      ? "● Online"
                      : "● Unavailable"}
                </span>
              </div>
              {loading && <div className="card-loader" />}
            </div>
          ))}
        </div>

        {/* Actions */}
        <div className="actions">
          <button
            className="refresh-btn"
            onClick={checkHealth}
            disabled={loading}
          >
            {loading ? "Refreshing…" : "🔄 Refresh Status"}
          </button>
        </div>
      </main>

      <footer className="app-footer">
        <p>Built with FastAPI · React · Docker</p>
      </footer>
    </div>
  );
}

export default App;
