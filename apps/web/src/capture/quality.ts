/**
 * FR-EXP-001's "glare and blur warning with retake prompt".
 *
 * Deliberately client-side, and deliberately BEFORE the shutter closes: ADR-031
 * §2 makes the point that a server judging blur could only reject an upload the
 * person has already walked away from. Here a retake is one tap, with the
 * receipt still in their hand.
 *
 * --- These WARN. They never refuse ---
 *
 * `analyse` returns findings; the screen shows them and offers a retake beside
 * a "use it anyway". A blurred photograph of a receipt is worth more than no
 * photograph of it, FR-EXP-001c is explicit that the product never blocks on
 * extraction, and these heuristics are wrong often enough that a hard refusal
 * would eventually throw away the only copy of something. The threshold
 * question is "is this worth a second look", not "is this readable".
 *
 * --- Why these two measures ---
 *
 * BLUR: the variance of a Laplacian convolution over the luminance channel.
 * The Laplacian responds to second-order intensity change, so a sharp receipt —
 * black text on white paper, thousands of hard edges — produces a wide spread
 * of responses, and a blurred one produces a narrow one. It is the standard
 * cheap measure and it needs one pass over the pixels.
 *
 * GLARE: the share of pixels at or near full brightness, which is what a
 * ceiling light or a flash on a glossy thermal receipt produces. Blown-out
 * pixels have lost their information entirely — no amount of later processing
 * recovers text from them — so the measure is area, not intensity.
 *
 * Both run on a DOWNSCALED greyscale copy (see `luminanceOf`), because a 12
 * megapixel frame would take long enough to make the capture screen stutter,
 * and neither measure needs the resolution: blur and glare are properties of
 * regions, not of individual pixels.
 *
 * --- What is NOT here ---
 *
 * FR-EXP-001 also names auto edge detection and deskew. Both are genuinely
 * harder — corner detection plus a perspective transform — and both are
 * image-processing work rather than interface work. MOB-002 puts the native
 * camera path in P2, which is where that belongs; this module is the part that
 * changes whether somebody re-takes a photograph, which is the part with the
 * product value in it. See ADR-036.
 */

/** Greyscale samples in 0..255, plus the shape they came from. */
export interface Luminance {
  readonly data: Uint8ClampedArray;
  readonly width: number;
  readonly height: number;
}

export type QualityFinding = "blurred" | "glare";

export interface QualityReport {
  readonly findings: readonly QualityFinding[];
  /** Laplacian variance. Higher is sharper. Reported so a threshold can be tuned against real images. */
  readonly sharpness: number;
  /** Share of blown-out pixels, 0..1. */
  readonly glare: number;
}

/**
 * Below this Laplacian variance, a receipt photograph is worth a second look.
 *
 * Chosen conservatively — far enough below a normal hand-held phone shot of
 * paper that an ordinary capture does not nag, because a warning that fires on
 * good photographs is one people learn to tap past, and then it is not there on
 * the day it was right.
 */
export const BLUR_THRESHOLD = 60;

/** Pixels at or above this are treated as blown out rather than merely bright. */
const BLOWN_OUT = 246;

/**
 * A tenth of the frame blown out is a reflection sitting on the receipt rather
 * than a bright room. White paper photographed well sits well below this: it is
 * bright, not clipped.
 */
export const GLARE_THRESHOLD = 0.1;

export function analyse(image: Luminance): QualityReport {
  const sharpness = laplacianVariance(image);
  const glare = blownOutShare(image.data);

  const findings: QualityFinding[] = [];
  if (sharpness < BLUR_THRESHOLD) findings.push("blurred");
  if (glare > GLARE_THRESHOLD) findings.push("glare");

  return { findings, sharpness, glare };
}

/**
 * Variance of the 4-neighbour Laplacian, over interior pixels only.
 *
 * The border is skipped rather than clamped: a clamped edge invents a zero
 * response all the way round the frame, which drags the variance down by an
 * amount that depends on the image's aspect ratio rather than on how sharp it
 * is — so a wide photograph would read as blurrier than a square one of the
 * same subject.
 */
export function laplacianVariance({ data, width, height }: Luminance): number {
  if (width < 3 || height < 3) return 0;

  let sum = 0;
  let sumOfSquares = 0;
  let count = 0;

  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const centre = y * width + x;
      const response =
        4 * (data[centre] ?? 0) -
        (data[centre - 1] ?? 0) -
        (data[centre + 1] ?? 0) -
        (data[centre - width] ?? 0) -
        (data[centre + width] ?? 0);
      sum += response;
      sumOfSquares += response * response;
      count += 1;
    }
  }

  if (count === 0) return 0;
  const mean = sum / count;
  return sumOfSquares / count - mean * mean;
}

export function blownOutShare(data: Uint8ClampedArray): number {
  if (data.length === 0) return 0;
  let blown = 0;
  for (const sample of data) if (sample >= BLOWN_OUT) blown += 1;
  return blown / data.length;
}

/** Longest edge of the working copy the measures run on. */
const ANALYSIS_SIZE = 480;

/**
 * An `ImageData`'s RGBA reduced to the luminance the measures want.
 *
 * Rec. 601 coefficients, which weight green highest because human vision does
 * — and because a receipt's contrast is ink against paper, which is a
 * luminance difference rather than a colour one. Averaging the channels
 * instead would under-weight exactly the signal being measured.
 */
export function toLuminance(image: ImageData): Luminance {
  const data = new Uint8ClampedArray(image.width * image.height);
  for (let index = 0; index < data.length; index += 1) {
    const rgba = index * 4;
    data[index] =
      0.299 * (image.data[rgba] ?? 0) +
      0.587 * (image.data[rgba + 1] ?? 0) +
      0.114 * (image.data[rgba + 2] ?? 0);
  }
  return { data, width: image.width, height: image.height };
}

/** The scale that brings an image's longest edge down to the analysis size. */
export function analysisScale(width: number, height: number): number {
  return Math.min(1, ANALYSIS_SIZE / Math.max(width, height));
}
