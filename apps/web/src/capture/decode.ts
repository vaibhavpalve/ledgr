/**
 * Turning a picked, dropped or photographed file into what the queue needs —
 * FR-EXP-001, FR-DOC-001.
 *
 * Separate from the capture screen because it is browser plumbing rather than
 * interface: a canvas, a bitmap decoder and a measurement. Keeping it out of
 * the `.tsx` file also keeps the screen's own source free of type aliases that
 * `scripts/check_translations.py` has to read past — its JSX-text heuristic
 * cannot tell `=> Promise<DecodedFile>` from a sentence, and the honest fix is
 * for components to hold components.
 */

import { analyse, toLuminance, type QualityReport } from "./quality";

export interface DecodedFile {
  readonly bytes: Uint8Array<ArrayBuffer>;
  readonly contentType: string;
  /** Null for anything with no single frame to measure — a PDF. */
  readonly quality: QualityReport | null;
}

export type DecodeFile = (file: File) => Promise<DecodedFile>;

/**
 * The browser implementation of `DecodeFile`.
 *
 * The ORIGINAL bytes are what gets queued — never a re-encoded canvas export.
 * FR-DOC-001 requires the original unaltered, and a JPEG round-tripped through
 * a canvas is a different file with different bytes and a different hash, which
 * would also defeat the archive's byte-identical duplicate detection.
 *
 * The canvas is used only to MEASURE, at a size the measures need rather than
 * the size the camera produced.
 */
export function browserDecode(analysisSize = 480): DecodeFile {
  return async (file: File): Promise<DecodedFile> => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const contentType = file.type || "application/octet-stream";
    if (!contentType.startsWith("image/")) return { bytes, contentType, quality: null };

    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, analysisSize / Math.max(bitmap.width, bitmap.height));
      const width = Math.max(3, Math.round(bitmap.width * scale));
      const height = Math.max(3, Math.round(bitmap.height * scale));

      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (context === null) return { bytes, contentType, quality: null };
      context.drawImage(bitmap, 0, 0, width, height);
      bitmap.close();

      return {
        bytes,
        contentType,
        quality: analyse(toLuminance(context.getImageData(0, 0, width, height))),
      };
    } catch {
      // A HEIC the browser cannot decode is the common case, and it is not a
      // reason to refuse the capture: the quality check is advice, and no
      // advice is better than no receipt.
      return { bytes, contentType, quality: null };
    }
  };
}
