# Phase 1B.2R — MP-3C3D4B completion report

Safe deep-research source acquisition and immutable structured-source
snapshot foundation. **This is NOT durable Deep Research.** No synthesis,
no public route, no worker sweep, no capability activation exists after
this phase. It is the acquisition/persistence foundation D4C will build on.

## Interrupted-session recovery

This session resumed from an interrupted state: four files
(`backend/app/config.py`, `backend/app/run_engine/research_sources.py`,
`backend/app/tools/research_search.py`, `backend/app/tools/research_fetch.py`)
had been created/edited in a prior session that expired before the
acquisition orchestrator, persistence primitive, tests, or documentation
were written. All four were re-inspected in full (not trusted from the
interrupted transcript) before any new code was added. `py_compile` on all
four passed immediately, but a full read surfaced three real defects,
fixed in this session:

1. **`backend/app/config.py`** — the 8 new `research_*` settings had NO
   positive-value validator at all (the existing `_validate_approval_limits`
   model-validator's field list didn't include any of them). Fixed by
   adding the 6 int-typed fields to that validator and a new
   `_validate_research_timeouts` validator for the 2 float fields
   (`research_fetch_timeout_s`, `research_total_deadline_s`), which also
   enforces the required cross-field invariant: a single fetch may never be
   allowed to consume the entire acquisition deadline
   (`research_fetch_timeout_s < research_total_deadline_s`).
2. **`backend/app/tools/research_fetch.py`** — `validate_fetch_url` did not
   normalize a redirect target before validating it, which (a) let a
   harmless case/fragment variation defeat redirect-loop detection and (b)
   left the persisted `final_url` non-canonical. Fixed by normalizing
   (reusing `research_search.normalize_result_url`) before validating, at
   every hop including the initial URL.
3. **`backend/app/tools/research_fetch.py`** — every SSRF/network exception
   was re-raised with `raise FetchError(...) from <original exception>`.
   `BrowserSecurityError` messages embed the resolved IP/host being
   rejected (including a private redirect target); `httpx` exception
   messages commonly embed the request URL. Chaining them would let that
   text reach any future `logging.exception()`/traceback dump via
   `exc.__cause__`, even though the raised `FetchError`'s own message is a
   bounded slug. Fixed: every one changed to `raise FetchError(...) from
   None`, matching the repository's own established convention for this
   exact concern (`app.run_engine.chat_stream`: "provider exception text is
   never propagated (`raise ... from None`)").

A fourth, test-only defect was also found and fixed while writing the
focused test suite: `neutralize_markers`'s case-insensitive regex
substitution used a **fixed lowercase replacement string**, which silently
rewrote the matched text's casing (e.g. `"[SAVE_DIRECTIVE"` → `"[save_directive"`)
— an unrequired alteration of factual page text, caught by a real
（not merely trivially-passing）test assertion. Fixed by replacing the
fixed-string substitution with a replacement **callback** that preserves
the original matched text's exact casing, only inserting the zero-width
separator.

## Skills used

- `superpowers:verification-before-completion` — every claim below is
  backed by a command run in this session; this discipline is what caught
  all four defects above (a naive test would have accepted the buggy
  `neutralize_markers` implementation).
- Ponytail (full mode) — kept the recovery narrow: read the four
  interrupted files in full before writing anything new; reused
  `RunPayload`'s existing envelope/`RunStep`'s existing unique constraint
  rather than proposing a migration; reused `httpx` (already a pinned
  dependency) and `httpx.MockTransport` (ships with it) rather than adding
  a new test-mocking dependency; reused `assert_url_allowed` rather than
  writing a second SSRF guard.
- No unrelated skill invoked; no parallel forks (a single sequential
  inspect-then-fix pass was cheaper for this size of recovery).

## Stage 0 evidence

```
git status --short   → dirty tree unchanged except the 4 interrupted files
                        (now completed) + this session's new files
git branch --show-current → phase-1b1-approval-domain
git rev-parse HEAD        → b2e67183d4791cbbaed9d3d98e8833983510f3ff
git diff --check          → clean
git stash list            → stash@{0}: 7ab31a5 (untouched)
```

Confirmed before and after this session's edits: `docs/superpowers/plans/2026-09-12-phase-1b2r-mp3c3d4a-durable-deep-research-plan.md`
exists (669 lines, non-empty); no `advance_deep_research_runs`/
`advance_research_runs` exists in `backend/app/run_engine/worker.py`;
`routes/runs.py` (line 135) and `routes/messages.py` (line 575) still call
`messages_stream.unsupported_mode_reason`/`unsupported_mode_code` against
the unchanged `_SUPPORTED_DURABLE_MODES = frozenset({"chat"})`; capability
still reports `deep_research` unavailable whenever durable cutover is on;
`run_messages_stream_durable_enabled=False`, `run_engine_enabled=False`; no
D4C or multi-agent file exists anywhere in the repository.

## Files changed

| File | Change |
|---|---|
| `backend/app/config.py` | 8 new `research_*` settings (bounds) + 2 new validators (int-bounds extension, float-timeout + cross-field invariant). |
| `backend/app/run_engine/payloads.py` | Added `"research_sources"` to `_VALID_TYPES` (additive; no schema change — `payload_type` is a plain `String(32)` column). |
| `backend/app/run_engine/research_sources.py` | New — versioned schema, bounds, deterministic normalization/dedup, citation correspondence, marker neutralization (fixed this session), progress-code set. Pure, DB-free, network-free. |
| `backend/app/run_engine/research_snapshot.py` | New — the encrypted persistence primitive (seal/open/replay), reusing `RunPayload`'s envelope and `RunStep`'s real DB unique constraint for at-most-one authority. |
| `backend/app/run_engine/research_acquisition.py` | New — the orchestrator: wires search+fetch adapters + `research_sources` normalization behind injectable cancel/session/deadline/heartbeat/progress seams. Zero DB commit, zero LLM/MCP/tool/KG/memory/directive call. |
| `backend/app/tools/research_search.py` | New — search adapter wrapping the existing free/local DuckDuckGo `search_web` (unmodified), adding URL normalization/dedup/rank. |
| `backend/app/tools/research_fetch.py` | New (fixed this session) — the guarded HTTP fetch/extract adapter: manual per-hop redirect validation, content-type allowlist, streamed byte cap, stdlib-only text extraction, injectable transport. |
| `tests/test_research_config.py` | New — 33 config-validation tests. |
| `tests/test_research_search_fetch.py` | New — 36 search/URL/SSRF/redirect/fetch/extract tests. |
| `tests/test_research_sources.py` | New — 33 schema/citation/dedup/marker tests. |
| `tests/test_research_acquisition.py` | New — 11 orchestrator tests (cancel/session/deadline/heartbeat/progress). |
| `tests/test_research_snapshot_persistence.py` | New — 11 persistence/replay/concurrency tests. |
| `tests/test_research_activation_boundary.py` | New — 8 tests reconfirming no route/capability/worker activation occurred. |

No migration. No frontend file touched. No new runtime dependency (`httpx`
is already pinned at `0.27.2`, matching the installed version exactly; its
own `MockTransport` — used only in tests — ships with the package).

## Selected transport and why

**`httpx` single-hop-at-a-time HTTP GET, NOT Playwright/headless Chromium.**
Chromium follows redirects and loads subresources (images, fonts, scripts,
XHR) internally with no request-interception anywhere in this repository
today (`web_scraper.py` and `browser_agent.py` both confirmed to never call
`page.route()`). Building genuine per-request validation over that surface
is exactly the "material new infrastructure" this phase was told to avoid.
A bare HTTP GET has no subresource problem at all — one GET is one
request — and lets every redirect be inspected and validated one hop at a
time before it is ever followed. This satisfies the "simpler guarded
HTTP fetch/extract path" alternative the task explicitly named.

**Residual, accepted limitation:** a guarded HTTP GET cannot execute
JavaScript, so client-rendered pages extract as sparse/empty text — an
extraction-quality tradeoff, not a security gap (the same class of
limitation `search_web`'s own snippets already have).

## SSRF and redirect proof

- **Every hop validated, not just the initial URL** — `validate_fetch_url`
  (normalizes, then calls `assert_url_allowed`) runs before the initial
  fetch AND before every redirect target is followed
  (`research_fetch.py::GuardedHttpFetchAdapter.fetch`).
- **Automatic redirect following is disabled**: `httpx.Client(follow_redirects=False, ...)`
  — proven by `test_automatic_redirect_following_is_disabled`, which shows
  the intermediate hop IS observed as a distinct request (if httpx followed
  automatically, the test's handler would never see it).
- **A public URL redirecting to a private IP is rejected before the second
  fetch** — proven by `test_public_url_redirecting_to_private_ip_rejected_before_second_fetch`:
  the handler raises `AssertionError` if the private target is ever
  requested, and it never fires.
- **Multi-hop redirects**: every hop validated
  (`test_multi_hop_redirect_all_hops_validated`).
- **Loop detection**: `test_redirect_loop_rejected`. **Count cap**:
  `test_redirect_count_cap_enforced` (`research_max_redirects`, validated
  positive in `config.py`).
- **Final effective URL is validated and canonical**
  (`test_final_effective_url_is_validated_and_returned`) — `final_url` is
  always the SAME normalized form `research_search.normalize_result_url`
  produces, so a fetched page's URL and a search result's URL are always
  comparable.
- **Scheme/userinfo/private-range rejection**: HTTP/HTTPS only; userinfo
  (`user:pass@host`) rejected (a check `assert_url_allowed` itself does not
  perform); loopback/link-local/private/reserved/multicast/unspecified +
  cloud metadata IPs rejected by the reused, proven
  `browser_agent.assert_url_allowed` (real `socket.getaddrinfo` resolution
  + `ipaddress` classification) — never a hand-rolled hostname blocklist.
  IPv4, IPv6, and IPv4-mapped IPv6 loopback/metadata forms all proven
  rejected (`test_validate_fetch_url_rejects_ipv6_loopback`,
  `..._ipv4_mapped_ipv6_loopback`, `..._ipv4_mapped_ipv6_metadata`). A
  public-looking hostname that DNS-resolves to a private IP is rejected
  (`test_validate_fetch_url_public_hostname_resolving_to_private_ip_rejected`).
- **No subresource/JS/download path exists in the transport** — proven
  structurally (`test_no_subresource_or_javascript_path_exists_in_transport`):
  a page whose HTML references external `<script>`/`<img>` results in
  EXACTLY ONE request to the handler.
- **Response size bounded during streaming, not after full buffering**:
  `resp.iter_bytes()` is consumed incrementally with a running length
  check that raises before the cap is exceeded
  (`test_response_byte_cap_enforced_during_streaming`) — httpx's
  `iter_bytes()` also yields already-decompressed content, so this
  incremental cap protects against a decompression bomb too, not only raw
  wire bytes.
- **Content-type allowlist** enforced before any body is read
  (`test_disallowed_content_type_rejected`).
- **Exceptions never chain sensitive text**: every `raise FetchError(...)`
  in this module uses `from None` (fixed this session) — proven by
  `test_fetch_timeout_bounded_and_redacted`'s explicit
  `exc_info.value.__cause__ is None` assertion.

**DNS-rebinding / TOCTOU — stated honestly, not glossed over:**
`assert_url_allowed` performs a real DNS resolution and IP classification
at *validation* time, but `httpx` performs its **own, independent** DNS
resolution when it later actually connects. There is a real, unclosed gap
between "the name we validated" and "the address the transport connects
to" for a TTL=0 DNS-rebinding attacker. **True pinning (validate, then
force the connection to that exact literal resolved address) is NOT
implemented anywhere in this repository** (confirmed: `browser_agent.py`'s
own `BrowserSession.navigate` has the identical characteristic — it calls
`assert_url_allowed` then lets Playwright do its own separate resolution
too). This module has the **same** residual exposure the existing,
already-shipped `assert_url_allowed`/`BrowserSession.navigate` path already
has — no better, no worse — recorded honestly in the module's own
docstring, not hidden or falsely claimed as solved.

## Snapshot schema, bounds, and citation contract

`research_sources.py`'s `RESEARCH_SOURCE_SNAPSHOT_VERSION = 1` — a bounded,
versioned dict: `research_snapshot_version`, `acquired_at`, `query_count`
(a bounded COUNT only — the raw query text never enters the snapshot),
`ordering` (`"search_rank"`), `total_passage_count`, `truncated` (true only
when a REAL configured bound — source count, passages-per-page, passage
chars, or total passage bytes — was actually reached; a page that merely
failed to fetch is NOT a truncation signal), and `sources[]` (each:
`source_id`, `url` — validated+canonical, `title`, `domain`, `search_rank`,
`retrieved_at`, `published_at` — only if actually known, `passages[]` —
each: `passage_id`, `marker`, `text`, `content_hash`).

**Exact citation invariant** — proven by dedicated tests, not merely
asserted: every surviving passage gets exactly one marker, assigned only
AFTER dedup/truncation (`test_every_passage_has_exactly_one_marker_and_markers_are_unique`);
an invalid-citation removal drops exactly the matching passage, never
others (`test_invalid_citation_removes_matching_passage_only`); a source
with zero surviving passages contributes no citation at all
(`test_removing_all_passages_drops_the_whole_source`); content hashes are
computed on the exact neutralized+truncated text that gets sealed
(`test_source_and_passage_ids_are_stable_and_content_hash_matches`,
`test_tampered_content_hash_rejected`).

**Deduplication policy (documented, tested, three layers deep):** (1)
identical canonical URL → second occurrence dropped
(`test_identical_url_second_occurrence_dropped`); (2) identical passage
content-hash from different URLs → second occurrence dropped, keeping
whichever source was encountered first
(`test_identical_passage_hash_from_different_urls_deduped_keeps_first`);
(3) the orchestrator ALSO dedupes candidate URLs before ever fetching them
(defense in depth, not a single point of failure).

**Marker neutralization** (fixed this session, see above): every known
reserved-marker opener (`app.skills.loader._MARKER_OPENS` — the SAME list
the model-output scanner uses, no second drifting definition) is broken by
inserting a zero-width space immediately after its first character,
preserving the exact original casing and every other character —
`test_marker_shaped_content_is_neutralized_and_no_longer_exact_matches`
(parametrized over all 7 execution-marker openers) and
`test_neutralize_markers_preserves_ordinary_text_untouched` prove this is
neutralization, not deletion or unrequested rewriting. This closes the gap
D4A identified: `chat_stream.MarkerScanner` only ever scans MODEL OUTPUT,
never retrieved INPUT — confirmed by inspection, this module is the
missing input-side boundary D4A called for.

**No raw HTML, secrets, cookies, or authorization headers are ever
stored** — the snapshot's source fields are a closed set (checked
structurally by `test_snapshot_fields_contain_no_html_tags`); the fetch
adapter takes no cookies/auth headers/browser profile at all.

## Persistence, replay, and concurrency proof

**At-most-one authority is a real SQL `UNIQUE` constraint, not a Python
check-then-set.** `RunPayload` itself has NO uniqueness constraint on
`(run_id, step_id, payload_type)` (confirmed: only plain, non-unique
indexes) — a naive persistence primitive built on that table alone would
NOT be PostgreSQL-safe. Instead, authority is anchored to
`RunStep`'s existing, real `UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq")`
(`models.py:1030`) — standard ANSI SQL, enforced identically by SQLite and
PostgreSQL. `seal_research_snapshot` seals the payload AND inserts a NEW
`RunStep` at `(run_id, seq)` in the SAME caller-owned, uncommitted
transaction (never commits internally). `test_second_seal_for_same_run_and_seq_is_rejected_by_the_real_unique_constraint`
proves this directly: a second concurrent seal attempt for the identical
`(run_id, seq)` raises a genuine `sqlalchemy.exc.IntegrityError` at commit
— not a soft check — and after rollback, the loser's `RunStep` AND
`RunPayload` rows are both completely absent (zero orphan state; the
winner is entirely unaffected). This is real evidence of PostgreSQL-safe
behavior (a standard UNIQUE constraint), not an assertion based only on
SQLite's own serialized-writer behavior.

**Replay reuses the sealed snapshot, zero new acquisition calls**:
`find_existing_research_step` + `open_research_snapshot` recover the exact
sealed bytes with no adapter invoked at all
(`test_replay_reads_existing_snapshot_without_any_adapter`).

**Fails closed on every binding mismatch**: wrong `run_id`
(`ResearchSnapshotBindingMismatch`), wrong step `kind`, missing output
payload, wrong `user_id`/`tenant_id` (via `RunPayload`'s own authenticated
envelope context check, `RunPayloadAccessError`/`RunPayloadContextMismatch`)
— all six proven with dedicated tests.

**Session-binding note (documented honestly, not overclaimed):** the
payload envelope binds `tenant_id/user_id/run_id/step_id/payload_type` — it
has **no** `session_id` field (a pre-existing characteristic shared by
every other `seal_payload` caller in the repo, e.g. `chat_stream`'s own
context snapshot). Session-family validation is NOT this primitive's job —
it is the orchestrator's `check_session_binding()` seam's job, exactly like
every other durable step already does it at the caller level via
`app.run_engine.session_binding.validate_session_binding`.

**Plaintext never appears outside the ciphertext**:
`test_plaintext_absent_from_the_raw_payload_row_and_step_columns` checks
the URL/title canaries are absent from the raw `ciphertext` column AND
every plain `RunStep` column (`input_ref`, `output_ref`, `error_code`).

## Cancellation / deadline / heartbeat seam behavior

`AcquisitionSeams` — every seam is REQUIRED (no silent no-op default), so a
caller can never accidentally run acquisition without cancel/session/
deadline coverage. Checked: before search, after search, before EACH
fetch, after EACH fetch, before building the final snapshot, and once more
before returning it to the caller (the last chance to abort before D4C's
future persistence call). Heartbeat renewed at the same points. Progress
(`research_searching` → `research_fetching` → `research_sources_ready`,
the closed set from D4-DECISION-1 — no `research_planning`) emitted at
exactly three points, enforced by an internal assertion
(`_emit`) as well as by tests. A per-page fetch failure is caught, bounded,
and skipped — never aborts the whole run (matches the legacy
`run_deep_research`'s own "one bad page must not abort research" posture).
Total search failure or zero results yields a truthful, valid, empty
snapshot — never an error, never a silent paid-provider fallback.
Cancel-before-search yields zero search/fetch calls
(`test_cancel_before_search_yields_zero_search_and_fetch_calls`);
cancel-between-fetches stops before any further page is fetched
(`test_cancel_between_fetches_prevents_later_fetch_calls`); session-revoke
and deadline-exceeded both stop identically
(`test_session_revoked_stops_acquisition`,
`test_deadline_exceeded_stops_acquisition`). This module performs **zero
DB commit, zero LLM call, zero MCP/tool/action execution, zero KG/memory/
directive write** — confirmed both by design (no such import exists in the
module) and by `test_orchestrator_module_has_no_llm_mcp_kg_memory_directive_imports`,
which asserts none of those symbols appear in the module's own source text.

## Exact focused test commands and counts

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_research_config.py \
  tests/test_research_search_fetch.py \
  tests/test_research_sources.py \
  tests/test_research_acquisition.py \
  tests/test_research_snapshot_persistence.py \
  tests/test_research_activation_boundary.py \
  -q
```
→ **132 passed, 0 failed** (33 config + 36 search/fetch + 33 sources +
11 acquisition + 11 persistence + 8 activation-boundary).

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_run_payloads.py \
  tests/test_settings_mode_capability.py \
  tests/test_run_engine_mp3c3d3a.py \
  tests/test_run_engine_mp3c3d1.py \
  -q
```
→ **110 passed, 0 failed** (directly-affected regression: payload store —
touched via the additive `_VALID_TYPES` entry — capability, and D3A/D1 mode
routing, all unaffected).

`py_compile` on all 7 changed/new backend files: clean. `git diff --check`:
clean. `import app.main`: OK.

## Deferred / residual

- **PostgreSQL live-server verification**: not run (SQLite in-memory only,
  per this phase's credit-economics constraint). The at-most-one-authority
  claim rests on a real ANSI SQL `UNIQUE` constraint (portable, standard
  behavior), not on SQLite-specific serialization — but the actual
  PostgreSQL server has not been exercised in this session.
- **Live network verification**: not run (mocked search/fetch only, per
  instructions). `DuckDuckGoSearchAdapter` and `GuardedHttpFetchAdapter`
  have never been exercised against the real internet in this phase.
- **DNS-rebinding/TOCTOU**: an honest, documented, UNCLOSED residual gap
  shared with the pre-existing `browser_agent.py` — not a regression
  introduced by this phase, but not solved by it either.
- No synthesis, no public durable-mode activation, no worker sweep, no
  frontend change, no capability activation — all correctly absent, per
  scope.

## D4C integration contract

D4C's future worker step should: (1) claim/lease a `deep_research` run
exactly like `advance_chat_runs` claims a `chat_stream` run; (2) build
`AcquisitionSeams` from the real worker/session-binding/lease mechanisms;
(3) call `run_research_acquisition(query, seams=..., search_adapter=DuckDuckGoSearchAdapter(), fetch_adapter=GuardedHttpFetchAdapter())`
inside its own transaction; (4) call `seal_research_snapshot(...,
commit=False)` with a deterministic `seq`, then commit atomically with
whatever else that step's transaction needs; (5) on an `IntegrityError` at
commit, roll back and call `find_existing_research_step` +
`open_research_snapshot` instead of assuming its own attempt won; (6) only
THEN implement `research_answer_synthesis`, extend
`_SUPPORTED_DURABLE_MODES`, and update the settings capability truth table
— none of which exist yet.
