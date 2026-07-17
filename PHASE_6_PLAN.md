# Phase 6 — Multi-User Authentication & Access Control

**Based on:** Phase 5 complete at commit `d735c5a` (tag: `phase-5-complete`)
**Integration branch:** `master`
**Created:** 2026-07-17

---

## Executive Summary

Replace the hardcoded `DEFAULT_USER_ID = 1` pattern with proper JWT-based authentication, enabling multi-user support across all backend endpoints and the frontend.

**Current state:** Every route (sessions, memories, messages) hardcodes `user_id = 1`. There is no login, registration, or session-based user isolation. The `User` model exists in SQLite but has no password field.

**Target state:** Users register with email + password, log in to receive a JWT token, and all API calls require a valid token. Each user sees only their own sessions, messages, and memories. The stream/router model selector remains available.

### Sub-Phase Breakdown

| Sub-Phase | Theme | Effort | Risk | Value |
|---|---|---|---|---|
| **6A** | Backend Auth Core | 2–3 days | Low | High (foundation) |
| **6B** | Authorization Middleware | 2–4 days | Medium | High (security) |
| **6C** | Frontend Auth UX | 2–3 days | Low | High (usability) |

**Total estimated effort:** 6–10 days for a single developer.

---

# PHASE 6A — Backend Authentication Core

## 6A-1. Exact Objective

Add JWT-based authentication infrastructure: password hashing, token creation/verification, and register/login/me endpoints. No existing routes are modified yet — this is purely additive.

## 6A-2. Current Problems Solved

| Problem | Severity | User Impact |
|---|---|---|
| No user authentication — anyone can call the API | High | No data isolation |
| Hardcoded `DEFAULT_USER_ID = 1` | Medium | Prevents multi-user |
| No password or email validation | Medium | Cannot extend to real users |
| User model has no password field | Low | Cannot authenticate |

## 6A-3. Dependencies Added

- `python-jose[cryptography]` — JWT creation and verification
- `passlib[bcrypt]` — Password hashing (bcrypt scheme)
- `python-multipart` — Already present (for form data)

Both are mature, well-audited libraries used in FastAPI production apps.

## 6A-4. Backend Changes

### 6A-4.1 User Model Update

Add `hashed_password` column to the `users` table:

```python
hashed_password = Column(String(255), nullable=True)  # nullable for existing default user
```

Existing default user (id=1, username="default") gets `hashed_password = None`. This is backward-compatible — the old `_create_default_user` path still works.

### 6A-4.2 Auth Service (`backend/app/services/auth_service.py`)

| Function | Purpose |
|---|---|
| `hash_password(password: str) -> str` | Hash with bcrypt |
| `verify_password(password: str, hashed: str) -> bool` | Verify bcrypt hash |
| `create_access_token(data: dict, expires_delta: Optional[timedelta]) -> str` | Create JWT with sub, exp, iat |
| `decode_access_token(token: str) -> dict` | Decode and verify JWT |
| `get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User` | FastAPI dependency that extracts user from Bearer token |

**Token config:**
- Algorithm: `HS256`
- Expiry: 7 days (configurable via `JWT_EXPIRY_HOURS`)
- Secret key: from `JWT_SECRET` env var, or auto-generated at startup (logged once, not recommended for production)

### 6A-4.3 Auth Schemas (`backend/app/schemas/auth.py`)

```python
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)

class LoginRequest(BaseModel):
    username: str = Field(...)
    password: str = Field(...)

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    created_at: datetime
```

### 6A-4.4 Auth Routes (`backend/app/routes/auth.py`)

| Method | Path | Purpose | Auth Required |
|---|---|---|---|
| `POST` | `/api/auth/register` | Create new user account | No |
| `POST` | `/api/auth/login` | Authenticate, return JWT | No |
| `GET` | `/api/auth/me` | Get current user info | Yes |

**Register flow:**
1. Validate username uniqueness
2. Validate email uniqueness
3. Hash password with bcrypt
4. Create user in database
5. Return user info (not the token — user must log in)

**Login flow:**
1. Find user by username
2. Verify password with bcrypt
3. Create JWT with `sub=user.id`, `exp=now+7d`
4. Return `{access_token, token_type: "bearer"}`

**Me flow:**
1. Extract token from `Authorization: Bearer ...`
2. Decode and verify JWT
3. Return user info

### 6A-4.5 Config Changes

Add to `backend/app/config.py`:
```python
jwt_secret: str = "change-me-in-production"  # overridden by JWT_SECRET env
jwt_algorithm: str = "HS256"
jwt_expiry_hours: int = 168  # 7 days
```

### 6A-4.6 Files Created (Phase 6A)

| File | Purpose |
|---|---|
| `backend/app/services/auth_service.py` | JWT + password utilities |
| `backend/app/routes/auth.py` | Register, login, me endpoints |
| `backend/app/schemas/auth.py` | Auth request/response schemas |
| `tests/test_auth.py` | 9+ backend auth tests |

### 6A-4.7 Files Modified (Phase 6A)

| File | Change |
|---|---|
| `backend/app/models/models.py` | Add `hashed_password` to `User` |
| `backend/app/main.py` | Add auth router; add column migration for `hashed_password` |
| `backend/app/config.py` | Add `jwt_secret`, `jwt_algorithm`, `jwt_expiry_hours` |
| `backend/requirements.txt` | Add `python-jose[cryptography]`, `passlib[bcrypt]` |
| `.env.example` | Add `JWT_SECRET` placeholder |

## 6A-5. Frontend Changes

**None.** The frontend is untouched in Phase 6A. The auth routes are purely backend additions. The existing frontend continues to work via the default user path.

Phase 6C will add frontend login/register UI.

## 6A-6. Database Changes

### 6A-6.1 Schema Migration

```sql
ALTER TABLE users ADD COLUMN hashed_password VARCHAR(255);
```

### 6A-6.2 Data Migration

The existing default user (id=1, username="default") gets `hashed_password = NULL`. This preserves backward compatibility — all existing API calls through the frontend continue to work because the frontend doesn't send auth headers yet.

## 6A-7. API Endpoints

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/api/auth/register` | Register a new user | No |
| `POST` | `/api/auth/login` | Log in, get JWT token | No |
| `GET` | `/api/auth/me` | Get current user from token | Yes |

## 6A-8. Automated Tests

### Backend Auth Tests (`tests/test_auth.py`)

| # | Test | What It Verifies |
|---|---|---|
| 1 | `test_register_user` | Successful registration returns user info |
| 2 | `test_register_duplicate_username` | Duplicate username returns 400 |
| 3 | `test_register_duplicate_email` | Duplicate email returns 400 |
| 4 | `test_register_password_too_short` | Password < 8 chars returns 422 |
| 5 | `test_login_success` | Valid credentials return access_token |
| 6 | `test_login_wrong_password` | Invalid password returns 401 |
| 7 | `test_login_nonexistent_user` | Unknown username returns 401 |
| 8 | `test_get_current_user` | Valid token returns user info |
| 9 | `test_get_current_user_invalid_token` | Bad token returns 401 |
| 10 | `test_get_current_user_expired_token` | Expired token returns 401 |

## 6A-9. Security Considerations

### 6A-9.1 JWT Secret Key
- **Risk:** Hardcoded or weak secret key
- **Mitigation:** Default is a placeholder; production MUST set `JWT_SECRET` env var
- **Mitigation:** Secret key validated at startup — warn if default is used
- **Mitigation:** HS256 with 256-bit random key recommended

### 6A-9.2 Password Storage
- **Risk:** Plaintext password in database
- **Mitigation:** bcrypt with 12 rounds (industry standard)
- **Mitigation:** Password never returned in any API response
- **Mitigation:** Password field excluded from `UserResponse` schema

### 6A-9.3 Token Expiry
- **Risk:** Stolen token used indefinitely
- **Mitigation:** 7-day expiry, configurable
- **Mitigation:** No refresh token in Phase 6 (future enhancement)

### 6A-9.4 Rate Limiting
- **Risk:** Brute-force password guessing on login
- **Mitigation:** Not implemented in Phase 6A (future enhancement)
- **Note:** Login endpoint returns generic "Invalid credentials" to avoid username enumeration

## 6A-10. Rollback Plan

**If 6A needs to be rolled back:**
1. `git revert <6A-commit-hash>`
2. `docker compose build backend && docker compose up -d`
3. Verify all Phase 5 endpoints work via default user path
4. Nullable `hashed_password` column remains but is unused
5. All 178 frontend tests and 99 backend tests still pass

**Rollback safety:** Adding nullable column is backward-compatible. Auth routes are new — no existing code depends on them.

## 6A-11. ✅ Definition Of Done

- [ ] `POST /api/auth/register` creates a new user with hashed password
- [ ] Registration validates username/email uniqueness and password strength
- [ ] `POST /api/auth/login` returns JWT token on valid credentials
- [ ] Login returns 401 on invalid credentials (generic error)
- [ ] `GET /api/auth/me` returns user info for valid token
- [ ] Auth endpoints use `python-jose[cryptography]` for JWT
- [ ] Auth endpoints use `passlib[bcrypt]` for password hashing
- [ ] Existing default user still works (hashed_password = NULL)
- [ ] All Phase 5 backend tests still pass
- [ ] All Phase 5 frontend tests still pass
- [ ] Security review: no passwords in responses, no stack traces, generic login errors
- [ ] Code review completed with zero critical findings
- [ ] No new secrets committed to source code

## 6A-12. Estimated Difficulty

| Feature | Difficulty | Effort | Dependencies |
|---|---|---|---|
| Password hashing + JWT utilities | Low | 0.5 day | passlib, python-jose |
| User model migration | Low | 0.5 day | Existing migration pattern |
| Auth routes (register, login, me) | Medium | 1 day | Utilities |
| Auth service integration | Low | 0.5 day | Config + DB |
| Testing | Medium | 1 day | Mock JWT verification |

**Total:** 3–4 days  
**Risk:** Low (purely additive, no existing code changed)
