# Phase 7 — Security Hardening & Production Reliability

**Based on:** Phase 6 complete at commit `1793648` (tag: `phase-6-complete`)
**Integration branch:** `master`
**Created:** 2026-07-17
**Audit reference:** `SECURITY_HARDENING.md`

---

## Executive Summary

Phase 7 focuses on hardening the application for production readiness. The Phase 6 audit identified 4 deployment-blocking issues (default JWT secret, no rate limiting, no CSP, no refresh tokens) and 14 additional findings ranging from medium to informational severity.

Phase 7 is organized into 5 small, independent checkpoints that can be implemented and verified separately. Each checkpoint builds on the previous but has its own rollback plan.

### Phase 6 Security Baseline

The following security properties are **already verified** and must be preserved:

- ✅ bcrypt password hashing (12 rounds)
- ✅ Generic "Invalid credentials" on login failure
- ✅ JWT `sub` claim is string (RFC 7519)
- ✅ Token expiry enforced (7-day default)
- ✅ Missing/malformed/expired tokens → 401
- ✅ Cross-user isolation on all routes
- ✅ No-token fallback to default user (backward compat)
- ✅ Sensitive content filter in memory extraction
- ✅ File upload validation (extension, MIME, size, path traversal)
- ✅ 0 frontend dependency vulnerabilities (`npm audit`)
- ✅ No secrets committed to Git

---

## Checkpoint Overview| Checkpoint | Theme | Effort | Risk | Value | Blocking? | Status ||---|---|---|---|---|---|---|| **7A** | Login rate limiting & abuse prevention | ✅ Done | — | ✅ Complete | — | ✅ Complete || **7B** | Refresh tokens & session management | 2-3 days | Medium | High | P1 | Not started || **7C** | Password reset & email verification | 3-4 days | Medium | Medium | P2 | Not started || **7D** | Production security headers & deployment hardening | 2-3 days | Low | High | P3 | Not started || **7E** | ChromaDB health & infrastructure reliability | 1-2 days | Low | Medium | P4 | Not started |

**Total estimated effort:** 9-14 days for a single developer.

---

# 7A — Login Rate Limiting & Abuse Prevention

## Scope

Add rate limiting to the login endpoint, implement temporary account lockout after repeated failed attempts, and validate JWT secret strength at startup.

## Acceptance Criteria — ✅ Met

- [x] Login endpoint returns 429 after N rapid failed attempts (configurable) — `rate_limit_max_attempts` (default 10)
- [x] Rate-limit headers (`Retry-After`) included in response
- [x] Account temporarily locked after M consecutive failed attempts (configurable) — `rate_limit_lockout_threshold` (default 5)
- [x] Lockout time increases exponentially (30s, 60s, 120s, 240s, 480s, capped at 900s)
- [x] JWT secret validated at startup; app refuses to start with default secret in production mode
- [x] All existing auth tests still pass (46/46, including 18 new Phase 7A tests)
- [x] No breaking changes to frontend auth flow

## Implementation Summary

**Branch:** `phase-7a-rate-limiting`

**Files Changed (7 files):**

| File | Change |
|---|---|
| `backend/app/services/rate_limiter.py` | **Created** — Abstract `RateLimiterInterface` + `InMemoryRateLimiter` with IP-key tracking, peek, cleanup, and `get_lockout_duration()` with exponential backoff |
| `backend/app/config.py` | Added `rate_limit_max_attempts`, `rate_limit_window_seconds`, `rate_limit_lockout_threshold`, `rate_limit_lockout_base_seconds`, `rate_limit_lockout_max_seconds`, `production_mode` |
| `backend/app/models/models.py` | Added `failed_login_attempts` (Integer, default=0) and `locked_until` (DateTime, nullable) columns to User |
| `backend/app/main.py` | Added `_validate_jwt_secret()` startup check; added Phase 7A DB migration; added `import logging`/`import sys` |
| `backend/app/routes/auth.py` | Added IP-based rate limiting to login and register; added account lockout with exponential backoff; failed-attempt counter reset on success; generic error preservation; sensitive-data-free logging |
| `backend/app/requirements.txt` | No new dependencies — custom `InMemoryRateLimiter` avoids `slowapi` |
| `tests/test_auth.py` | Added 18 Phase 7A tests: rate limiter unit tests, IP rate limit integration tests, account lockout tests, registration rate limit tests, GET endpoint isolation test |
| `.env.example` | Documented all 7 rate limit env vars |

## Database Migration (automatic)

```sql
ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until TIMESTAMP;
```

## Configuration Variables

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_MAX_ATTEMPTS` | `10` | Max auth attempts per time window per IP |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Time window in seconds |
| `RATE_LIMIT_LOCKOUT_THRESHOLD` | `5` | Consecutive failures before account lockout |
| `RATE_LIMIT_LOCKOUT_BASE_SECONDS` | `30` | Base lockout duration (doubles each time) |
| `RATE_LIMIT_LOCKOUT_MAX_SECONDS` | `900` | Max lockout duration (15 min) |
| `PRODUCTION_MODE` | `false` | Enforces JWT secret strength at startup |

## Tests Added (19 new + 15 memory-growth = 34 new tests)

| Class | Tests | What It Verifies |
|---|---|---|
| `TestRateLimitUnit` | 7 | Lockout duration math, peek doesn't record, cleanup expired, reset, is_rate_limited records |
| `TestLoginRateLimit` | 4 | IP rate limit exceeded, Retry-After header, shared IP across users, reset on success |
| `TestAccountLockout` | 2 | Lockout after threshold failures, reset after successful login |
| `TestRegistrationRateLimit` | 2 | Registration rate limited, Retry-After header |
| `TestGetEndpointsNotRateLimited` | 1 | GET /health works after hitting rate limit |
| `TestJWTSecretValidation` | 1 | Lockout duration unit test |

**Existing tests preserved:** All 28 Phase 6 auth tests still pass unchanged.

## Known Limitations (Resolved)

- ~~`peek_rate_limit` does not prune stored entries~~ ✅ Fixed: write-through `_prune_key` now reassigns or removes empty keys
- ~~`record_attempt` has no pruning~~ ✅ Fixed: accepts optional `window_seconds` parameter (default 60), prunes before appending
- ~~No automatic `cleanup_expired` scheduling~~ ✅ Fixed: probabilistic auto-cleanup after every 100 mutations (instance-level configurable `cleanup_interval`)
- ~~No thread-safety~~ ✅ Fixed: `threading.RLock` protects all public methods; safe for concurrent login attempts
- ~~No graceful shutdown~~ ✅ Fixed: `stop()` abstract method added; InMemory impl clears state
- ~~No `reset_rate_limiter` helper for tests~~ ✅ Fixed: replaces singleton with fresh instance

**Remaining limitation:** Single-process only — `InMemoryRateLimiter` is not suitable for multi-worker/multi-instance deployments. The `RateLimiterInterface` abstract class allows replacing with Redis.

## Rollback

1. `git revert <7A-commit-hash>`
2. `docker compose cp` or rebuild container
3. No data loss; rate limiting removed; columns remain in DB but unused
4. All existing tests still pass

## Definition of Done — ✅ Complete

- [x] Login endpoint rate-limited with configurable threshold (10/60s)
- [x] Account lockout with exponential backoff (5 failures, 30s → 900s max)
- [x] JWT secret validation at startup (warns in dev, refuses in production)
- [x] All tests pass: 46 auth (18 new + 28 existing) + 94 Phase 5 backend + 216 frontend
- [x] TypeScript clean | Production build succeeds
- [x] No frontend changes needed

---

# 7B — Refresh Tokens & Session Management

## Scope

Implement refresh token rotation, server-side token tracking, and logout token invalidation. Reduce access token lifetime and add refresh token with longer expiry.

## Acceptance Criteria

- [ ] Access token lifetime reduced to 15 minutes (configurable)
- [ ] Refresh token with 30-day lifetime (configurable)
- [ ] `POST /api/auth/refresh` endpoint returns new access + refresh tokens
- [ ] Refresh token rotation: old refresh token invalidated after use
- [ ] `POST /api/auth/logout` invalidates current token server-side
- [ ] Token blacklist stored in SQLite (auto-cleanup expired entries)
- [ ] Frontend auto-refreshes token when access token expires
- [ ] All existing tests still pass
- [ ] Backward compatibility: old 7-day tokens still work until expiry

## Files Likely to Change

| File | Change |
|---|---|
| `backend/app/schemas/auth.py` | Add `RefreshRequest`, `RefreshResponse` schemas |
| `backend/app/routes/auth.py` | Add `/api/auth/refresh` and `/api/auth/logout` endpoints |
| `backend/app/services/auth_service.py` | Add `create_refresh_token`, `verify_refresh_token`, `blacklist_token` |
| `backend/app/models/models.py` | Add `TokenBlacklist` model |
| `backend/app/config.py` | Add `jwt_access_expiry_minutes`, `jwt_refresh_expiry_days` |
| `frontend/src/auth.ts` | Add refresh-token storage; auto-refresh on 401 |
| `frontend/src/api.ts` | Intercept 401, attempt refresh, retry request |
| `frontend/src/types.ts` | Add refresh types |
| `tests/test_auth.py` | Add refresh token tests |
| `.env.example` | Document refresh token env vars |

## Database Migration

```sql
CREATE TABLE token_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jti VARCHAR(255) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_token_blacklist_expires ON token_blacklist(expires_at);
```

## Tests Required

| # | Test | What It Verifies |
|---|---|---|
| 1 | `test_refresh_token_success` | Valid refresh token returns new access token |
| 2 | `test_refresh_token_rotation` | Old refresh token invalidated after use |
| 3 | `test_refresh_token_expired` | Expired refresh token returns 401 |
| 4 | `test_logout_invalidates_token` | Token blacklisted after logout |
| 5 | `test_blacklisted_token_rejected` | Blacklisted token returns 401 |
| 6 | `test_old_access_token_still_works` | Existing tokens not broken |
| 7 | `test_blacklist_auto_cleanup` | Expired blacklist entries removed |

## Security Risks

- **Risk:** Refresh token stolen → long-term access
  - **Mitigation:** Rotation ensures each refresh token used once; if stolen token is used, legitimate user's token is invalidated
- **Risk:** Token blacklist grows unbounded
  - **Mitigation:** Auto-cleanup on every write; TTL index

## Rollback

1. `git revert <7B-commit-hash>`
2. Token blacklist table remains but is unused
3. All access tokens revert to 7-day lifetime
4. Frontend refresh logic removed

## Definition of Done

- [ ] Access token 15-min lifetime; refresh token 30-day
- [ ] `/api/auth/refresh` and `/api/auth/logout` endpoints
- [ ] Token blacklist with auto-cleanup
- [ ] Frontend auto-refresh on 401
- [ ] All tests pass (existing + new)

---

# 7C — Password Reset & Email Verification

## Scope

Implement email-based password reset flow and email verification after registration. Add rate limiting to password reset requests.

## Acceptance Criteria

- [ ] `POST /api/auth/forgot-password` sends password-reset email
- [ ] `POST /api/auth/reset-password` resets password with time-limited token
- [ ] Password-reset token expires in 1 hour
- [ ] Password-reset token can only be used once
- [ ] `POST /api/auth/verify-email` verifies email with signed token
- [ ] Rate limiting on forgot-password (1 request per 60 seconds per email)
- [ ] Generic response on forgot-password (don't reveal if email exists)
- [ ] No frontend changes required (Phase 7C is backend + email integration only)

## Files Likely to Change

| File | Change |
|---|---|
| `backend/app/schemas/auth.py` | Add `ForgotPasswordRequest`, `ResetPasswordRequest`, `VerifyEmailRequest` |
| `backend/app/routes/auth.py` | Add forgot-password, reset-password, verify-email endpoints |
| `backend/app/services/auth_service.py` | Add `create_reset_token`, `verify_reset_token` |
| `backend/app/models/models.py` | Add `email_verified`, `email_verification_token` to User |
| `backend/app/config.py` | Add `reset_token_expiry_minutes`, `email_config` |
| `backend/requirements.txt` | Add email library (e.g., `aiosmtplib` or SendGrid SDK) |
| `tests/test_auth.py` | Add password-reset and email tests |
| `.env.example` | Document email config vars |

## Database Migration

```sql
ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN email_verification_token VARCHAR(255);
```

## Tests Required

| # | Test | What It Verifies |
|---|---|---|
| 1 | `test_forgot_password_returns_200` | Generic success response |
| 2 | `test_forgot_password_nonexistent_user` | Same generic response (no enumeration) |
| 3 | `test_reset_password_success` | Valid token resets password |
| 4 | `test_reset_password_expired_token` | Expired token returns 401 |
| 5 | `test_reset_password_reused_token` | One-time use enforced |
| 6 | `test_forgot_password_rate_limit` | 429 after rapid requests |
| 7 | `test_verify_email` | Valid token marks email verified |

## Security Risks

- **Risk:** Email provider credentials in source → token theft
  - **Mitigation:** Use env vars only; never commit email credentials
- **Risk:** Reset token leaked in logs → account takeover
  - **Mitigation:** Never log reset tokens; use generic log messages

## Rollback

1. `git revert <7C-commit-hash>`
2. Email columns remain but are unused
3. Password reset endpoints removed
4. All existing auth tests still pass

## Definition of Done

- [ ] Password-reset flow with time-limited, single-use tokens
- [ ] Email verification flow (backend only)
- [ ] Rate limiting on forgot-password
- [ ] All tests pass (existing + new)

---

# 7D — Production Security Headers & Deployment Hardening

## Scope

Add Content Security Policy, CORS configuration, request size limits, Markdown sanitization, error message sanitization, and validate JWT secret at startup.

## Acceptance Criteria

- [ ] Content Security Policy header restricts script/style sources
- [ ] CORS explicitly configured with allowed origins (not `*`)
- [ ] Request body size limited (configurable, default 10MB)
- [ ] Markdown rendered content sanitized (no raw HTML injection)
- [ ] Error messages filtered: no internal paths, source code, or secrets
- [ ] JWT secret validated at startup (app refuses to start with default)
- [ ] All existing tests still pass
- [ ] No breaking changes to frontend behavior

## Files Likely to Change

| File | Change |
|---|---|
| `backend/app/main.py` | Add CSP middleware, CORS middleware, request size limit middleware |
| `backend/app/routes/documents.py` | Sanitize error messages (remove internal paths) |
| `frontend/package.json` | Add `rehype-sanitize` |
| `frontend/src/MarkdownRenderer.tsx` | Add `rehype-sanitize` plugin |
| `frontend/index.html` | Add CSP meta tag (optional, backend middleware is primary) |
| `backend/app/config.py` | Add `max_request_size_mb`, `allowed_origins` |
| `.env.example` | Document CORS origins |
| `tests/test_security.py` | Add CSP, CORS, size-limit tests |

## Tests Required

| # | Test | What It Verifies |
|---|---|---|
| 1 | `test_csp_header_present` | Response includes `Content-Security-Policy` |
| 2 | `test_cors_allowed_origin` | Configured origin returns correct CORS headers |
| 3 | `test_cors_denied_origin` | Unknown origin blocked |
| 4 | `test_request_size_limit` | Oversized request returns 413 |
| 5 | `test_error_message_no_internal_paths` | Error details sanitized |
| 6 | `test_markdown_sanitized` | XSS attempt blocked in rendered Markdown |

## Security Risks

- **Risk:** CSP too restrictive → blocks legitimate inline scripts
  - **Mitigation:** Use nonce-based CSP; test thoroughly before deployment
- **Risk:** CORS too permissive → cross-origin requests allowed
  - **Mitigation:** Explicit whitelist; no wildcard in production

## Rollback

1. `git revert <7D-commit-hash>`
2. CSP/CORS/size-limit middleware removed
3. Markdown rendering returns to unsanitized (pre-7D state)
4. All existing tests still pass

## Definition of Done

- [ ] CSP header restricts script/style sources
- [ ] CORS configured with explicit allowed origins
- [ ] Request body size limited
- [ ] Markdown sanitized (XSS protection)
- [ ] Error messages sanitized
- [ ] JWT secret validated at startup
- [ ] All tests pass (existing + new)

---

# 7E — ChromaDB Health & Infrastructure Reliability

## Scope

Fix ChromaDB health check, add health-check retry logic, ensure container startup ordering, and add infrastructure monitoring.

## Acceptance Criteria

- [ ] ChromaDB container health check passes consistently
- [ ] Backend retries ChromaDB connection on startup (not just fails immediately)
- [ ] Docker compose `depends_on` uses `condition: service_healthy` for ChromaDB
- [ ] Health-check timeout and interval tuned to ChromaDB startup time
- [ ] Backend health endpoint reports ChromaDB status accurately
- [ ] Logging: ChromaDB connection errors logged with context
- [ ] All existing tests still pass

## Files Likely to Change

| File | Change |
|---|---|
| `docker-compose.yml` | Fix ChromaDB health check command; add `condition: service_healthy` |
| `backend/app/main.py` | Add startup retry for ChromaDB connection |
| `backend/app/services/chromadb_client.py` | Add connection retry with exponential backoff |
| `.env.example` | Document ChromaDB timeout settings |

## Tests Required

| # | Test | What It Verifies |
|---|---|---|
| 1 | `test_chromadb_health_endpoint` | `/api/health` reports chromadb status |
| 2 | `test_chromadb_connection_retry` | Backend retries on connection failure |
| 3 | `test_health_check_timing` | Health check does not hang > 5s |

## Security Risks

- **Risk:** None direct; infrastructure reliability only

## Rollback

1. `git revert <7E-commit-hash>`
2. Docker compose returns to previous health check
3. Backend connection retry removed
4. All existing tests still pass

## Definition of Done

- [ ] ChromaDB health check passes consistently
- [ ] Backend retries ChromaDB connection with backoff
- [ ] Docker compose uses healthy condition
- [ ] All tests pass (existing + new)

---

## Dependencies Between Checkpoints

```
7A (rate limiting) ── independent ──┐
7B (refresh tokens) ── independent ──┤
7C (password reset) ── depends on 7B ─┤  → All merge to master
7D (security headers) ── independent ──┤
7E (infra reliability) ── independent ──┘
```

- **7A, 7B, 7D, 7E** are fully independent and can be implemented in any order
- **7C** ideally follows 7B (refresh token infrastructure provides mailer patterns)
- All checkpoints merge directly to `master`

## Rollback Strategy

Each checkpoint is self-contained, small, and easily reverted:
1. `git revert <checkpoint-commit-hash>`
2. `docker compose build backend && docker compose up -d`
3. Run full test suite
4. Verify no regression

## Definition of Done (Phase 7)

- [ ] All 5 checkpoints implemented and verified
- [ ] All existing tests pass (216 frontend + 122 backend)
- [ ] New tests added for each checkpoint
- [ ] SECURITY_HARDENING.md findings addressed (or documented as accepted risk)
- [ ] TypeScript check passes
- [ ] Production build succeeds
- [ ] Browser smoke test passes (auth flow + features)

---

## Appendix: Vulnerability Tracking

| SECURITY_HARDENING.md ID | Phase 7 Checkpoint | Status |
|---|---|---|
| C-001 | 7D (startup validation) + 7A (startup validation) | Planned |
| H-001 | 7A | Planned |
| H-002 | 7D | Planned |
| H-003 | 7B | Planned |
| M-001 | 7D (CSP mitigation) | Planned |
| M-002 | 7A | Planned |
| M-003 | 7D | Planned |
| M-004 | 7D | Planned |
| M-005 | 7D | Planned |
| M-006 | 7E | Planned |
| M-007 | 7C | Planned |
| M-008 | 7C | Planned |
| L-001 | 7A | Planned |
| L-002 | Not planned (upstream library issue) | Accepted |
| L-003 | 7D | Planned |
| L-004 | 7D | Planned |
| L-005 | 7E | Planned |
| L-006 | Informational — not actionable | Accepted |
| I-001 | Informational — not actionable | Accepted |
| I-002 | 7D (CSP covers this) | Planned |
| I-003 | 7B (reduced to 15 min) | Planned |
| I-004 | 7B | Planned |
| I-005 | Informational — not in scope | Accepted |
