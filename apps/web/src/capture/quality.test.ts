import { describe, expect, it } from "vitest";

import {
  BLUR_THRESHOLD,
  GLARE_THRESHOLD,
  analyse,
  analysisScale,
  blownOutShare,
  laplacianVariance,
  toLuminance,
  type Luminance,
} from "./quality";

/**
 * Synthetic images, because the properties being asserted are the measures'
 * own — a sharp edge produces a wide spread of Laplacian responses, a flat
 * field produces none — and a photograph of a real receipt would test the
 * photograph rather than the measure.
 */

function grey(width: number, height: number, fill: (x: number, y: number) => number): Luminance {
  const data = new Uint8ClampedArray(width * height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) data[y * width + x] = fill(x, y);
  }
  return { data, width, height };
}

/**
 * Alternating hard stripes: the closest synthetic thing to printed text.
 *
 * Paper is 240, not 255. Real receipt paper photographed well is bright and
 * NOT clipped — using pure white here would make every sharp fixture trip the
 * glare measure, and the test would then be asserting that a good photograph
 * is a bad one.
 */
const sharpText = grey(64, 64, (x) => (x % 4 < 2 ? 20 : 240));

/** The same subject through a lens that has lost focus — a smooth ramp. */
const blurred = grey(64, 64, (x) => 128 + 40 * Math.sin((x / 64) * Math.PI));

describe("laplacianVariance — FR-EXP-001's blur warning", () => {
  it("is high for an image full of hard edges", () => {
    expect(laplacianVariance(sharpText)).toBeGreaterThan(BLUR_THRESHOLD);
  });

  it("is near zero for a flat field, which has no detail at all", () => {
    expect(laplacianVariance(grey(32, 32, () => 128))).toBeCloseTo(0);
  });

  it("is low for a smoothly varying image", () => {
    expect(laplacianVariance(blurred)).toBeLessThan(BLUR_THRESHOLD);
  });

  it("ranks a sharp image above a blurred one", () => {
    // The property that actually matters — the absolute number is a tuning
    // decision, the ordering is the measure being correct.
    expect(laplacianVariance(sharpText)).toBeGreaterThan(laplacianVariance(blurred));
  });

  it("does not let a wide image read as blurrier than a square one of the same subject", () => {
    // The reason the border is skipped rather than clamped: a clamped edge
    // invents a zero response all the way round, and how much that drags the
    // variance down depends on the aspect ratio.
    const wide = grey(128, 16, (x) => (x % 4 < 2 ? 0 : 255));
    const square = grey(48, 48, (x) => (x % 4 < 2 ? 0 : 255));

    expect(laplacianVariance(wide)).toBeCloseTo(laplacianVariance(square), 0);
  });

  it("is zero for an image too small to have an interior", () => {
    expect(laplacianVariance(grey(2, 2, () => 200))).toBe(0);
  });
});

describe("blownOutShare — FR-EXP-001's glare warning", () => {
  it("is zero for well-exposed white paper, which is bright but not clipped", () => {
    expect(blownOutShare(grey(32, 32, () => 235).data)).toBe(0);
  });

  it("counts pixels that have lost their information entirely", () => {
    const half = grey(32, 32, (x) => (x < 16 ? 255 : 120));

    expect(blownOutShare(half.data)).toBeCloseTo(0.5);
  });

  it("is zero for an empty image rather than dividing by zero", () => {
    expect(blownOutShare(new Uint8ClampedArray())).toBe(0);
  });
});

describe("analyse", () => {
  it("finds nothing wrong with a sharp, well-exposed frame", () => {
    expect(analyse(sharpText).findings).toEqual([]);
  });

  it("warns about blur", () => {
    expect(analyse(blurred).findings).toContain("blurred");
  });

  it("warns about glare", () => {
    // A sharp image with a reflection across a third of it: the blur measure
    // is satisfied and the glare measure is not.
    const withGlare = grey(64, 64, (x, y) => (y < 22 ? 255 : x % 4 < 2 ? 20 : 240));

    expect(analyse(withGlare).findings).toContain("glare");
  });

  it("can report both at once", () => {
    // A reflection across the top of an otherwise out-of-focus frame. The ramp
    // below it is linear, and the Laplacian of a linear function is zero — so
    // this is blurred by the measure as well as blown out.
    const bad = grey(64, 64, (_x, y) => (y < 40 ? 255 : 250 - (y - 40) * 2));

    expect(analyse(bad).findings).toEqual(expect.arrayContaining(["blurred", "glare"]));
  });

  it("reports the raw measures, so a threshold can be tuned against real photographs", () => {
    const report = analyse(sharpText);

    expect(report.sharpness).toBeGreaterThan(0);
    expect(report.glare).toBeGreaterThanOrEqual(0);
    expect(GLARE_THRESHOLD).toBeGreaterThan(0);
  });
});

describe("toLuminance", () => {
  it("weights green highest, because a receipt's contrast is a luminance difference", () => {
    const rgba = new Uint8ClampedArray([0, 255, 0, 255, 0, 0, 255, 255]);
    const image = { data: rgba, width: 2, height: 1, colorSpace: "srgb" } as ImageData;

    const { data } = toLuminance(image);

    expect(data[0]).toBeGreaterThan(data[1]!);
  });

  it("keeps the image's shape", () => {
    const rgba = new Uint8ClampedArray(4 * 6);
    const image = { data: rgba, width: 3, height: 2, colorSpace: "srgb" } as ImageData;

    expect(toLuminance(image)).toMatchObject({ width: 3, height: 2 });
  });
});

describe("analysisScale", () => {
  it("shrinks a camera frame to something the measures can run on quickly", () => {
    expect(analysisScale(4000, 3000)).toBeCloseTo(480 / 4000);
  });

  it("never enlarges an image that is already small", () => {
    expect(analysisScale(200, 150)).toBe(1);
  });
});
