import { describe, expect, it, vi } from "vitest";
import { registerServiceWorker } from "./registerServiceWorker";

describe("registerServiceWorker", () => {
  it("does not throw when the browser has no serviceWorker (jsdom's default)", async () => {
    expect("serviceWorker" in navigator).toBe(false);
    await expect(registerServiceWorker()).resolves.toBeUndefined();
  });

  it("registers sw.js when the browser supports it", async () => {
    const register = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "serviceWorker", {
      value: { register },
      configurable: true,
    });

    try {
      await registerServiceWorker();
      expect(register).toHaveBeenCalledWith("/sw.js");
    } finally {
      delete (navigator as { serviceWorker?: unknown }).serviceWorker;
    }
  });

  it("swallows a registration failure rather than throwing", async () => {
    Object.defineProperty(navigator, "serviceWorker", {
      value: { register: vi.fn().mockRejectedValue(new Error("nope")) },
      configurable: true,
    });

    try {
      await expect(registerServiceWorker()).resolves.toBeUndefined();
    } finally {
      delete (navigator as { serviceWorker?: unknown }).serviceWorker;
    }
  });
});
