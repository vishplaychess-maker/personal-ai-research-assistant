/**
 * Development-only Vite proxy targeting (Phase 1B.2R MP-3C3D6B).
 *
 * The committed dev proxy points at `http://backend:8080`, which is the Docker
 * Compose service name. The deterministic browser smoke runs the backend as a
 * host process, so it needs to retarget the dev proxy at a loopback origin.
 *
 * Rules, all enforced here rather than in `vite.config.ts`:
 *
 * - Unset / empty  -> the committed default, returned byte-for-byte.
 * - The override is read from `process.env.THUNDER_DEV_PROXY_TARGET`, which is
 *   a NODE-side variable. It is deliberately not exposed to browser code and is
 *   never read from the client-side Vite env object, so it cannot leak into a
 *   production bundle: this function only affects `server.proxy` in the dev
 *   server configuration.
 * - Only bare loopback http/https origins are accepted. Credentials, query
 *   strings, fragments and non-http(s) schemes are rejected outright.
 */

export const DEFAULT_DEV_PROXY_TARGET = "http://backend:8080";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

// Deliberately NOT using the global `URL` constructor: `vite.config.ts` is
// type-checked by a Node-only tsconfig that has no DOM lib, and this module is
// pulled in by it. A small anchored pattern keeps the validator dependency-free
// and equally strict.
const ORIGIN_PATTERN = /^(https?):\/\/([^/?#\s]+)(\/[^?#\s]*)?$/;

function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(host) || host.startsWith("127.");
}

/** Extract and validate the host from `host`, `host:port` or `[ipv6]:port`. */
function hostFromAuthority(authority: string): string {
  if (authority.includes("@")) {
    throw new Error("credentials are not allowed in THUNDER_DEV_PROXY_TARGET");
  }
  if (authority.startsWith("[")) {
    const end = authority.indexOf("]");
    if (end === -1) {
      throw new Error("invalid IPv6 host in THUNDER_DEV_PROXY_TARGET");
    }
    const rest = authority.slice(end + 1);
    if (rest !== "" && !/^:\d+$/.test(rest)) {
      throw new Error("invalid port in THUNDER_DEV_PROXY_TARGET");
    }
    return authority.slice(1, end);
  }
  const colon = authority.lastIndexOf(":");
  if (colon === -1) return authority;
  const port = authority.slice(colon + 1);
  if (port !== "" && !/^\d+$/.test(port)) {
    throw new Error("invalid port in THUNDER_DEV_PROXY_TARGET");
  }
  return authority.slice(0, colon);
}

/**
 * Resolve the dev-server proxy target from a raw environment value.
 * Throws on anything that is not a bare loopback http/https origin.
 */
export function resolveDevProxyTarget(raw: unknown): string {
  if (raw === undefined || raw === null) return DEFAULT_DEV_PROXY_TARGET;
  if (typeof raw !== "string") {
    throw new Error("THUNDER_DEV_PROXY_TARGET must be a string");
  }
  const value = raw.trim();
  if (value === "") return DEFAULT_DEV_PROXY_TARGET;

  if (value.includes("?")) {
    throw new Error("a query string is not allowed in THUNDER_DEV_PROXY_TARGET");
  }
  if (value.includes("#")) {
    throw new Error("a fragment is not allowed in THUNDER_DEV_PROXY_TARGET");
  }
  const match = ORIGIN_PATTERN.exec(value);
  if (match === null) {
    throw new Error(
      "THUNDER_DEV_PROXY_TARGET must be an absolute http/https origin",
    );
  }
  const host = hostFromAuthority(match[2]);
  if (host === "") {
    throw new Error("THUNDER_DEV_PROXY_TARGET must include a host");
  }
  if (!isLoopbackHost(host)) {
    throw new Error(
      "THUNDER_DEV_PROXY_TARGET must be a loopback host (127.0.0.1, localhost or ::1)",
    );
  }
  return value;
}
