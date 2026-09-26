import { describe, expect, it } from "vitest";

import {
  DEFAULT_DEV_PROXY_TARGET,
  resolveDevProxyTarget,
} from "../lib/devProxyTarget";

describe("resolveDevProxyTarget", () => {
  it("keeps the committed default byte-for-byte when unset or empty", () => {
    expect(DEFAULT_DEV_PROXY_TARGET).toBe("http://backend:8080");
    expect(resolveDevProxyTarget(undefined)).toBe("http://backend:8080");
    expect(resolveDevProxyTarget(null)).toBe("http://backend:8080");
    expect(resolveDevProxyTarget("")).toBe("http://backend:8080");
    expect(resolveDevProxyTarget("   ")).toBe("http://backend:8080");
  });

  it("accepts bare loopback http/https origins", () => {
    expect(resolveDevProxyTarget("http://127.0.0.1:8080")).toBe("http://127.0.0.1:8080");
    expect(resolveDevProxyTarget("http://localhost:18080")).toBe("http://localhost:18080");
    expect(resolveDevProxyTarget("https://[::1]:8443")).toBe("https://[::1]:8443");
    expect(resolveDevProxyTarget("  http://127.0.0.1:9000  ")).toBe("http://127.0.0.1:9000");
  });

  it("rejects a non-loopback host", () => {
    expect(() => resolveDevProxyTarget("http://example.com:80")).toThrow(/loopback/i);
    expect(() => resolveDevProxyTarget("http://10.0.0.5:8080")).toThrow(/loopback/i);
    expect(() => resolveDevProxyTarget("http://backend:8080")).toThrow(/loopback/i);
  });

  it("rejects credentials, query strings and fragments", () => {
    expect(() => resolveDevProxyTarget("http://user:pw@127.0.0.1:8080")).toThrow();
    expect(() => resolveDevProxyTarget("http://user@127.0.0.1:8080")).toThrow();
    expect(() => resolveDevProxyTarget("http://127.0.0.1:8080/?q=1")).toThrow(/query/i);
    expect(() => resolveDevProxyTarget("http://127.0.0.1:8080/#frag")).toThrow(/fragment/i);
  });

  it("rejects non-http(s) schemes and unparsable values", () => {
    expect(() => resolveDevProxyTarget("ftp://127.0.0.1:21")).toThrow(/http/i);
    expect(() => resolveDevProxyTarget("file:///etc/passwd")).toThrow(/http/i);
    expect(() => resolveDevProxyTarget("not-a-url")).toThrow();
    expect(() => resolveDevProxyTarget("//127.0.0.1:8080")).toThrow();
  });

  it("rejects non-string values instead of coercing them", () => {
    expect(() => resolveDevProxyTarget(8080 as unknown)).toThrow();
    expect(() => resolveDevProxyTarget({} as unknown)).toThrow();
    expect(() => resolveDevProxyTarget(["http://127.0.0.1:1"] as unknown)).toThrow();
  });

  it("exposes only the default constant and the pure resolver", async () => {
    // Keeping the module side-effect free is what guarantees the override stays a
    // Node-side development concern: there is no exported config object and no
    // client-env read anywhere in it. (The absence of any client-env reference is
    // additionally verified by a repository search in the D6B verification step.)
    const mod = await import("../lib/devProxyTarget");
    expect(Object.keys(mod).sort()).toEqual([
      "DEFAULT_DEV_PROXY_TARGET",
      "resolveDevProxyTarget",
    ]);
  });
});
