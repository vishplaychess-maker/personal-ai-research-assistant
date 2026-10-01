---
name: video-generation
description: Generate short AI videos from a text prompt via Venice AI (primary), NexaAPI, or Agnes AI, with automatic provider fallback and async polling.
pinned: false
---
# Video Generation

**Description:** Generate short AI videos from a text prompt via Venice AI (primary), NexaAPI, or Agnes AI, with automatic provider fallback and async polling.

## When to use
- The user asks to "generate / create / make a video" or "animate" from a description.

## How it works in Thunder AI
Video generation runs through the **Media Generation panel** (sidebar → image/video icon)
and the REST API. This skill is informational: it does **not** execute generation
itself. When a request matches, point the user at the Media Generation panel, or
call the API below on their behalf if you have an authenticated context.

- `POST /api/media/generate-video` — body: `{ prompt, provider?, model?, duration? }` → returns `{ task_id, provider }`
- `GET  /api/media/video/{task_id}?provider={provider}&wait=true` — long-poll until `status` is `completed` / `failed`
- `GET  /api/media/providers` — lists providers and which have an API key configured

Auth + CSRF required. Rate limit: 10 req/min per user. Generation takes ~30–300s;
the poll times out at `MEDIA_VIDEO_TIMEOUT_S` (default 300s).

## Providers & models
| Provider | Env var | Video models | Cost |
|----------|---------|--------------|------|
| Venice AI (primary) | `VENICE_API_KEY` | `seedance-2-0-text-to-video-basic`, `seedance-2-5-text-to-video-basic` | ~$0.10–0.50/video |
| NexaAPI | `NEXA_API_KEY` | `kling-video`, `ltx-video` | $5 free credits |
| Agnes AI | `AGNES_API_KEY` | `agnes-video-2.5-flash`, `agnes-video-v2.0` | free tier, 1–5 RPM |

Fallback order: Venice → Nexa → Agnes. A provider with no API key is skipped.

## Parameters
| Param | Default | Notes |
|-------|---------|-------|
| prompt | — | required; sanitized server-side |
| provider | server primary (`venice`) | `venice` \| `nexa` \| `agnes` |
| model | provider default | see table above |
| duration | `5s` | `3s` \| `5s` \| `10s` |

## Response
Start returns `{ success, task_id, provider }`. Poll returns
`{ success, url, status, error? }` — `url` is a direct video URL for inline playback.

## Notes
- Provider API keys are **server-side env vars**, not per-user encrypted keys.
- The panel polls to completion and adds the result to the chat transcript automatically.
