# Changelog

All notable changes to the Personal AI Research Assistant.

## [Unreleased]

### F6 — Thoughtful Agent UX

#### Capability 2: Self-evaluation / confidence score (advisory)
- Added evaluation service (`backend/app/services/evaluation_service.py`) that asks the
  LLM to score how confident it is in its answer (`0-100` plus a one-line reason).
  Fully defensive: any parse/provider failure falls back to a null score and never
  breaks the chat.
- Added the `self_evaluate` LangGraph node between answer generation and save
  (`backend/app/services/langgraph_workflow.py`). Confidence is advisory and never
  blocks or alters the answer.
- Persisted `confidence` + `confidence_reason` on assistant `Message` rows
  (new nullable columns + startup migration), including on the streaming chat path.
- Added a color-coded `ConfidenceBadge` to assistant messages in the UI
  (`frontend/src/ConfidenceBadge.tsx`): green (80-100), amber (50-79), red (0-49),
  hidden when the score is null.
- New tests: `tests/test_evaluation.py` (10 passing).

#### Capability 1: Plan-then-execute (review-only preview)
- Added plan generation (`backend/app/services/planning_service.py`) that proposes
  a step-by-step plan before an answer, surfaced to the user as a review-only
  `PlanCard` (`frontend/src/PlanCard.tsx`). Execution of the plan is a v1 no-op
  (planned for a later release).
- New `plan` SSE event in the streaming chat path.
- New tests: `tests/test_planning.py` (10 passing).

### Bug fixes
- Fixed "hi" (or any first message) getting no reply: the default Ollama chat model
  was `llama3.2:3b`, which was not installed on the host Ollama, so every request
  returned `Ollama HTTP 404: model ... not found` and no assistant message was saved.
  The default is now `dolphin-mistral`, which is actually installed
  (`backend/app/config.py`). The stream now completes and the response is persisted.
