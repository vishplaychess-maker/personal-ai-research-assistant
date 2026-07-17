# Security Hardening Audit

**Date:** 2026-07-17
**Scope:** Personal AI Research Assistant (Phase 1-6)
**Audit type:** Manual codebase review + automated dependency checks
**Tools used:** `npm audit`, frontend/backend test suites, manual code review

---

## Executive Summary

The application implements reasonable authentication and authorization for a development/research prototype but has several areas that require hardening before production deployment. The most critical gaps are the lack of login rate limiting, the default JWT secret, and the absence of Content Security Policy (CSP) headers.

**Automated scan results:**
- Frontend dependencies: **0 vulnerabilities** (`npm audit --omit=dev`)
- Backend dependencies: Not audited (requires `pip-audit` package)
- Git secrets: No `.env` file or secrets committed to repository
- Auth tests: 28/28 passing
- No credentials, tokens, or API keys in source code

---

## Findings by Severity

### 🔴 Critical (1)

| ID | Finding | Location | Evidence | Remediation | Blocks Deploy? |
|---|---|---|---|---|---|
| **C-001** | **Default JWT secret used when `JWT_SECRET` env var is not set** | `backend/app/config.py:21` | `jwt_secret: str = "change-me-in-production"` | Validate at startup; refuse to start with default secret in production. Use `os.urandom(32).hex()`. | Yes |

### 🟠 High (4)

| ID | Finding | Location | Evidence | Remediation | Blocks Deploy? |
|---|---|---|---|---|---|
| **H-001** | **No rate limiting on login endpoint** | `backend/app/routes/auth.py` — `login_user()` | ✅ **Fixed in Phase 7A** — InMemoryRateLimiter with configurable threshold (10/60s), Retry-After headers, account lockout, and exponential backoff | Add `slowapi` middleware with per-IP rate limiting (e.g., 5 attempts/min). **Custom InMemoryRateLimiter used instead of slowapi for simplicity.** | ✅ Fixed |
| **H-002** | **No Content Security Policy (CSP) headers** | `backend/app/main.py`, frontend config | No CSP middleware in FastAPI; no `meta` CSP tag in `index.html` | Add CSP via FastAPI middleware or Vite plugin to restrict script/style sources. | Yes |
| **H-003** | **No refresh token mechanism** | `backend/app/services/auth_service.py` | JWT access token expires in 7 days with no refresh flow | Implement refresh tokens (7-day access + 30-day refresh with rotation). | Yes |
| **H-004** | **SQLite database file exposed in Docker volume** | `docker-compose.yml` | `app_data:/data` volume contains `app.db` with user data | Ensure volume is not exposed externally; document backup procedures. | No (Docker internal only) |

### 🟡 Medium (8)

| ID | Finding | Location | Evidence | Remediation | Blocks Deploy? |
|---|---|---|---|---|---|
| **M-001** | **Token stored in localStorage (XSS-vulnerable)** | `frontend/src/auth.ts` | `localStorage.setItem(TOKEN_KEY, token)` | Consider httpOnly cookies for production; implement token binding. | No (standard SPA pattern) |
| **M-002** | **No brute-force/account lockout protection** | `backend/app/routes/auth.py` | ✅ **Fixed in Phase 7A** — Account lockout after 5 consecutive failures with exponential backoff (30s–900s); counter resets on successful login | Implement exponential backoff and temporary account lockout after N failed attempts. | ✅ Fixed |
| **M-003** | **react-markdown renders user/AI content as HTML** | `frontend/src/MarkdownRenderer.tsx` | `react-markdown` renders LLM output as HTML with `remark-gfm` | Sanitize output with `rehype-sanitize`; verify all links are safe. | No |
| **M-004** | **Error messages may leak internal paths** | `backend/app/routes/documents.py` | `doc.error_message = str(exc)[:500]` | Truncate and filter internal paths, source code snippets, and stack traces. | No |
| **M-005** | **No request body size limit** | `backend/app/main.py` | No `max_request_size` middleware configured | Add `FastAPI` middleware to limit POST body size (e.g., 10MB). | No |
| **M-006** | **Ollama endpoint accessible from backend container** | `docker-compose.yml` | `OLLAMA_HOST=host.docker.internal:11434` | Verify network isolation; consider requiring API keys for Ollama. | No |
| **M-007** | **No password-reset flow** | Missing feature | No endpoint or UI for password reset | Implement email-based reset flow with time-limited tokens. | No |
| **M-008** | **No email verification** | Missing feature | No email verification after registration | Send verification email with signed token. | No |

### 🔵 Low (6)

| ID | Finding | Location | Evidence | Remediation | Blocks Deploy? |
|---|---|---|---|---|---|
| **L-001** | **Default user (id=1) has no password** | `backend/app/main.py:72` | `_create_default_user()` creates user with no `hashed_password` | Deprecate default user; require all new deployments to register. | No |
| **L-002** | **JWT `iat` claim uses timezone-aware UTC; `jose` uses deprecated `utcnow()`** | `backend/app/services/auth_service.py:56` | `datetime.now(timezone.utc)` vs `jose` warning | Accept `DeprecationWarning` as non-blocking; upstream library issue. | No |
| **L-003** | **No CORS configuration (FastAPI default allows all)** | `backend/app/main.py` | No `CORSMiddleware` added | Explicitly configure allowed origins for production. | No (Docker proxy restricts) |
| **L-004** | **File upload filename stored as-is** | `backend/app/routes/documents.py` | `doc.filename = file.filename` retains original name | Sanitize display name; use UUID for storage. | No |
| **L-005** | **ChromaDB container shows as unhealthy** | `docker compose ps` | `research-assistant-chromadb (unhealthy)` | Debug health check; adjust timeout or command. | No |
| **L-006** | **Passwords in test files** | `tests/test_auth.py` | Test passwords like `securePass123!` in source | Not a real concern; test data in test files is standard practice. | No |

### ℹ️ Informational (5)

| ID | Finding | Location | Evidence | Notes |
|---|---|---|---|---|
| **I-001** | `loginUser()` makes two API calls (login + /me) | `frontend/src/auth.ts:49-56` | Login then immediately calls `/me` | Consider returning user info from login endpoint |
| **I-002** | No `helmet`-equivalent for FastAPI | Missing | No security headers middleware | Consider `fastapi-secure-headers` or manual middleware |
| **I-003** | Long-lived token (7 days) | `backend/app/config.py:22` | `jwt_expiry_hours: int = 168` | Acceptable for research tool; reduce for production |
| **I-004** | No token blacklist on logout | Missing | Logout only clears client-side state | Implement server-side token invalidation for production |
| **I-005** | No user role/permission model | Missing | All authenticated users have same permissions | Add for multi-tenant deployments |

---

## Automated Test Coverage for Security

| Test Suite | Tests | What it covers | Status |
|---|---|---|---|
| `test_auth.py` — Registration | 7 | Uniqueness, password strength, username validation | ✅ 7/7 |
| `test_auth.py` — Login | 5 | Correct/wrong password, nonexistent user, generic errors | ✅ 5/5 |
| `test_auth.py` — Token validation | 5 | Valid, missing, malformed, expired, ghost user | ✅ 5/5 |
| `test_auth.py` — Cross-user isolation | 14 | Session, messages, memories, search, documents | ✅ 14/14 |
| `test_auth.py` — Rate limiting (Phase 7A) | 32 | IP rate limit, lockout, memory growth, concurrent safety, cleanup | ✅ 32/32 |
| `test_streaming.py` — Error details | 1 | Error messages must not contain secrets | ✅ 1/1 |
| `test_memories.py` — Sensitive filter | 3 | Passwords, API keys filtered from memory extraction | ✅ 3/3 |

---

## Phase 7A Completion Summary

**Branch:** `phase-7a-rate-limiting`
**Commits:** `094436d` (feat) + `ebf4030` (fix)
**Tests:** 63 auth tests (32 Phase 7A + 28 Phase 6 + 3 helpers) — all passing

### Resolved Findings

| ID | Finding | Severity | Resolution |
|---|---|---|---|
| **H-001** | No rate limiting on login | 🟠 High | ✅ Custom `InMemoryRateLimiter` with configurable 10/60s threshold, `Retry-After` headers, dual IP + account limiting |
| **M-002** | No brute-force/account lockout | 🟡 Medium | ✅ Account lockout after 5 consecutive failures with exponential backoff (30s → 900s max); counter resets on success |

### Additional Reliability Fixes

- **Thread-safety:** `threading.RLock` protects all public methods; verified with concurrent 20-thread × 50-call stress test
- **Bounded memory:** Write-through `_prune_key` removes empty keys; `record_attempt` prunes before append; probabilistic auto-cleanup every 100 mutations
- **Graceful shutdown:** `stop()` abstract method added; InMemory impl clears state
- **Redis-ready interface:** `RateLimiterInterface` unchanged with `stop()` added for future Redis implementation

### Remaining Phase 7 Work

| Checkpoint | Scope | Effort | Status |
|---|---|---|---|
| **7B** | Refresh tokens & session management | 2-3 days | Not started |
| **7C** | Password reset & email verification | 3-4 days | Not started |
| **7D** | Production security headers & deployment hardening | 2-3 days | Not started |
| **7E** | ChromaDB health & infrastructure reliability | 1-2 days | Not started |

---

## Deployment Blockers

The following findings **must** be addressed before production deployment:

1. **C-001**: Default JWT secret
2. **H-001**: No rate limiting on login
3. **H-002**: No Content Security Policy
4. **H-003**: No refresh token mechanism

These four items represent actual attack vectors (token theft, brute-force login, XSS/script injection, session hijacking) that could compromise user data.

---

## Quick Wins (Low Effort, High Impact)

1. **Validate JWT secret at startup** — 2 lines of code
2. **Add request size limit middleware** — 3 lines of code
3. **Configure CORS explicitly** — 5 lines of code
4. **Add CSP meta tag to `index.html`** — 1 line
5. **Set `sameSite` and `secure` considerations** — documentation only
6. **Sanitize Markdown output** — add `rehype-sanitize` plugin

---

## Recommended Implementation Order

| Priority | Phase | Items |
|---|---|---|
| P0 | 7A — Login rate limiting | H-001, M-002 (brute force + lockout) | ✅ **Complete** |
| P1 | 7B — Refresh tokens | H-003, I-004 (refresh + token blacklist) |
| P2 | 7C — Password reset & email verification | M-007, M-008 |
| P3 | 7D — Production hardening | C-001, H-002, M-003, M-004, M-005, L-003, L-004 |
| P4 | 7E — Infrastructure reliability | L-005 (ChromaDB health) |
