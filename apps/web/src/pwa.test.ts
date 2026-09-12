/**
 * §5.2/MOB-002's installable PWA - the parts that are plain files rather than
 * runtime behaviour, verified the way the task's own instructions describe:
 * the manifest is valid JSON, is linked from index.html, and declares what
 * `display: "standalone"` installability needs.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function repoFile(relativeToSrc: string): string {
  return readFileSync(fileURLToPath(new URL(relativeToSrc, import.meta.url)), "utf-8");
}

describe("the installable PWA shell", () => {
  it("manifest.webmanifest is valid JSON with what standalone installability needs", () => {
    const manifest = JSON.parse(repoFile("../public/manifest.webmanifest")) as {
      name: string;
      short_name: string;
      start_url: string;
      display: string;
      background_color: string;
      theme_color: string;
      icons: { src: string; sizes: string; type: string }[];
    };

    expect(manifest.name).toBe("LEDGR");
    expect(manifest.short_name).toBe("LEDGR");
    expect(manifest.display).toBe("standalone");
    expect(typeof manifest.start_url).toBe("string");
    expect(typeof manifest.background_color).toBe("string");
    expect(typeof manifest.theme_color).toBe("string");

    const sizes = manifest.icons.map((icon) => icon.sizes);
    expect(sizes).toContain("192x192");
    expect(sizes).toContain("512x512");
    expect(manifest.icons.every((icon) => icon.type === "image/png")).toBe(true);
  });

  it("index.html links the manifest", () => {
    const html = repoFile("../index.html");
    expect(html).toMatch(/<link\s+rel="manifest"\s+href="\/manifest\.webmanifest"\s*\/>/);
  });

  it("sw.js exists and only handles GET requests outside /v1/", () => {
    const sw = repoFile("../public/sw.js");
    expect(sw).toContain('addEventListener("fetch"');
    expect(sw).toContain("/v1/");
  });
});
