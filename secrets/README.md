# Executor-service credentials (MP-4C)

This directory holds the **only** credential the executor service accepts. It is
`gitignore`d in full except this README and `*.example`.

## Create it (before the first start)

```bash
cp secrets/executor_service_tokens.json.example secrets/executor_service_tokens.json
# then replace the placeholder with a long random token
```

Format: ONE shared secret, at least 16 bytes, used as the HMAC signing key for
the backend→executor **execution grant** (`backend/mp4c_contract/grant.py`).

```
<one long random shared secret>
```

The grant carries the identity INSIDE its signature, so a caller cannot choose a
user or a tenant: the request body has no `user_id`/`tenant_id` field at all.
This replaces the earlier per-user token map, which could only represent one
identity per token and therefore could not serve arbitrary authenticated users.

## Why a file rather than an environment variable

Docker Compose mounts a file secret at `/run/secrets/executor_service_tokens`
(inside the executor container only). The value therefore never appears in:

* `docker compose config` output,
* the container's `Config.Env` (visible to `docker inspect`),
* `ps` / `/proc/*/environ` listings,
* the service's logs or error responses.

An environment variable would leak the token into at least the first two.

The loader reads the file first and treats it as **authoritative**: if the
configured file exists but is unreadable or malformed, the registry is EMPTY and
every request is rejected. There is no silent fallback to the environment.

## Rotation

Replace the file and restart the executor service. There is no hot reload and no
token expiry in this slice; a token is valid until the file changes.
