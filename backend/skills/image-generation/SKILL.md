# Image Generation

**Description:** Generate AI images from a text prompt via Venice AI (primary), NexaAPI, or Agnes AI, with automatic provider fallback.

## When to use
- The user asks to "generate / create / make an image", "draw", "visualize", or wants concept art, illustrations, or diagrams.

## How it works in Thunder AI
Image generation runs through the **Media Generation panel** (sidebar → image/video icon)
and the REST API. This skill is informational: it does **not** execute generation
itself. When a request matches, tell the user to open the Media Generation panel,
or call the API below on their behalf if you have an authenticated context.

- `POST /api/media/generate-image` — body: `{ prompt, provider?, model?, width?, height? }`
- `GET  /api/media/providers` — lists providers and which have an API key configured

Auth + CSRF required (same as every other write endpoint). Rate limit: 10 req/min per user.

## Providers & models
| Provider | Env var | Image models | Cost |
|----------|---------|--------------|------|
| Venice AI (primary) | `VENICE_API_KEY` | `flux-schnell`, `flux-pro`, `flux-2-max` | ~$0.01–0.03/image |
| NexaAPI | `NEXA_API_KEY` | `flux-schnell`, `flux-pro` | ~$0.001–0.003/image, $5 free credits |
| Agnes AI | `AGNES_API_KEY` | `agnes-image-2.1-flash` | free tier, 20 RPM |

Fallback order: Venice → Nexa → Agnes. A provider with no API key is skipped.

## Parameters
| Param | Default | Notes |
|-------|---------|-------|
| prompt | — | required; sanitized server-side (injection patterns stripped, 4000 char cap) |
| provider | server primary (`venice`) | `venice` \| `nexa` \| `agnes` |
| model | provider default | see table above |
| width / height | 1024 | 256–2048 |

## Response
`{ success, url, provider, model, error? }` — `url` is a direct image URL for inline display.

## Notes
- Provider API keys are **server-side env vars**, not per-user encrypted keys.
- The panel adds the result to the chat transcript automatically.
