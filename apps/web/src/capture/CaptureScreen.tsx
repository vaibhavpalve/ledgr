import { useCallback, useEffect, useRef, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseCategoryKey } from "@ledgr/shared-types";
import type { CaptureSource } from "@ledgr/offline-queue";
import { Camera, Upload } from "lucide-react";

import "./CaptureScreen.css";
import { CategoryPicker } from "./CategoryPicker";
import type { DecodeFile } from "./decode";
import type { QualityReport } from "./quality";
import { UploadsOverview } from "./UploadsOverview";
import type { CaptureInput, Sitting } from "./useSitting";
import { useModalFocus } from "../useModalFocus";
import type { CaptureQueue } from "@ledgr/offline-queue";

/**
 * FR-EXP-001's capture screen — camera and upload, in one place.
 *
 *     FR-EXP-001  Receipt capture by CAMERA (single tap from the home screen,
 *                 multi-page, auto edge detection, deskew, glare and blur
 *                 warning with retake prompt) and by UPLOAD (drag-and-drop or
 *                 file picker, accepting JPEG, PNG, HEIC, PDF and multi-page
 *                 PDF). Both paths land in the same place.
 *
 * --- "Both paths land in the same place" is one function here too ---
 *
 * The camera, the file picker and the drop zone all end in `accept`. `source`
 * is recorded and read by nothing, exactly as on the server (ADR-031 §2): two
 * pipelines would be two chances to skip the quality check, and the one built
 * second is the one that skips it.
 *
 * --- The two "several" are two buttons, and never a guess ---
 *
 *     "Another page of this receipt"  → joins the current receipt
 *     "Next receipt"                  → starts a new one
 *
 * Nothing infers this from elapsed time or similarity. ADR-031 makes the case
 * at length: a heuristic would be wrong SILENTLY, in the direction that either
 * multiplies or merges somebody's claim.
 *
 * --- The format list is not repeated here ---
 *
 * The file input's `accept` is a convenience for the picker, not a control.
 * SEC-005's allowlist is verified from the bytes by the document archive and
 * this screen does not re-decide it (ADR-031 §4) — a second list would drift,
 * and the one that drifted would be the one a receipt was wrongly refused by.
 *
 * --- The one tap FR-EXP-001 asks for is the shutter, not "Start" ---
 *
 * The sitting starts itself, in the background, the moment this screen
 * mounts — there is no separate "Start" tap to make first (ADR-047). The
 * shutter button is on screen and tappable from the very first render, before
 * the session has necessarily finished opening. `useSitting`'s `capture()` is
 * what makes that safe rather than lossy: a tap landing while the session is
 * still opening waits for it instead of silently dropping the photograph. See
 * `useSitting.ts` for that half of the fix.
 */
export function CaptureScreen({
  sitting,
  queue,
  decode,
}: {
  sitting: Sitting;
  queue: CaptureQueue;
  /**
   * Turns a file into bytes and, where it is an image, a quality reading.
   *
   * Injected because decoding needs a `<canvas>` and a `createImageBitmap`
   * that jsdom does not have — and because the screen's real behaviour is what
   * it does with the ANSWER, which is the part worth testing.
   */
  decode: DecodeFile;
}) {
  const { t } = useI18n();
  const fileInput = useRef<HTMLInputElement>(null);
  const cameraInput = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<PendingCapture | null>(null);
  const [joining, setJoining] = useState(false);
  // Files chosen but not yet filed: the category is asked before anything is
  // decoded or queued. See `receive`.
  const [awaitingCategory, setAwaitingCategory] = useState<AwaitingCategory | null>(null);
  const [dragging, setDragging] = useState(false);

  // FR-EXP-001's "single tap from the home screen": the sitting starts itself
  // rather than waiting for a "Start" tap, so the only tap left is the
  // shutter's. Guarded by a ref rather than by `sitting.phase` alone — the
  // effect re-runs whenever `sitting` changes identity (every phase
  // transition), and the ref is what stops that from calling `start()` again
  // on the next one.
  const autoStarted = useRef(false);
  useEffect(() => {
    if (!autoStarted.current && sitting.phase === "idle") {
      autoStarted.current = true;
      void sitting.start();
    }
  }, [sitting]);

  /**
   * Every file, in order, stopping at the first one worth a second look.
   *
   * Strictly in order, and the REST is held rather than dropped. Both matter:
   * page order is the order somebody photographed a document in, and a drop of
   * five files where the second is blurred must not silently swallow the last
   * three — which is what returning early would do.
   *
   * `into` is decided once, for the whole batch. Dropping four files while
   * "another page" is armed means four pages of this receipt, which is what a
   * four-page invoice scanned in one go is.
   */
  const accept = useCallback(
    async (
      files: readonly File[],
      source: CaptureSource,
      into: string | undefined,
      category: ExpenseCategoryKey | undefined,
    ) => {
      for (let index = 0; index < files.length; index += 1) {
        const file = files[index]!;
        const decoded = await decode(file);
        const input: CaptureInput = {
          bytes: decoded.bytes,
          contentType: decoded.contentType,
          filename: file.name || null,
          source,
          ...(into === undefined ? {} : { into }),
          ...(category === undefined ? {} : { category }),
        };

        // FR-EXP-001's glare and blur warning, before the capture is committed
        // to. A PDF gets no reading — there is no frame to measure — and goes
        // straight through.
        if (decoded.quality !== null && decoded.quality.findings.length > 0) {
          setPending({
            input,
            quality: decoded.quality,
            rest: files.slice(index + 1),
            source,
            into,
            category,
          });
          return;
        }
        await sitting.capture(input);
      }
      setJoining(false);
    },
    [decode, sitting],
  );

  /** Resume a batch that stopped on a warning, with or without the flagged page. */
  const resume = useCallback(
    async (pendingCapture: PendingCapture, keep: boolean) => {
      setPending(null);
      if (keep) await sitting.capture(pendingCapture.input);
      if (pendingCapture.rest.length > 0) {
        await accept(
          pendingCapture.rest,
          pendingCapture.source,
          pendingCapture.into,
          pendingCapture.category,
        );
      } else {
        setJoining(false);
      }
    },
    [accept, sitting],
  );

  const armedFor = (): string | undefined =>
    joining && sitting.currentReceiptRef !== null ? sitting.currentReceiptRef : undefined;

  /**
   * Where every path in ends: the camera, the picker and a drop.
   *
   * A NEW receipt is filed before it goes anywhere - the category is asked for
   * first and the files wait for the answer. A further page of a receipt that
   * already exists (`armedFor`) skips the question, because that receipt has
   * its category and asking again would invite filing one invoice two ways.
   */
  const receive = (files: readonly File[], source: CaptureSource) => {
    if (files.length === 0) return;
    const into = armedFor();
    if (into !== undefined) {
      void accept(files, source, into, undefined);
      return;
    }
    setAwaitingCategory({ files, source });
  };

  // Stable, because the picker's Escape listener is re-registered whenever its
  // `onCancel` changes identity.
  const cancelCategory = useCallback(() => setAwaitingCategory(null), []);

  const onFiles = (event: React.ChangeEvent<HTMLInputElement>, source: CaptureSource) => {
    const files = [...(event.target.files ?? [])];
    // Cleared so that picking the same file twice in a row still fires a
    // change event — a person retaking a photograph of the same receipt is an
    // ordinary thing to do.
    event.target.value = "";
    receive(files, source);
  };

  if (sitting.phase === "finalised") {
    return (
      <section className="capture" aria-label={t("capture.screen.title")}>
        <h1>{t("capture.screen.title")}</h1>
        <p role="status" data-testid="capture-finalised">
          {t("capture.screen.finalised")}
        </p>
        <UploadsOverview sitting={sitting} queue={queue} />
      </section>
    );
  }

  return (
    <section className="capture" aria-label={t("capture.screen.title")}>
      <h1>{t("capture.screen.title")}</h1>
      <SittingProblemNotice sitting={sitting} />

      {pending !== null ? (
        <QualityPrompt
          report={pending.quality}
          onRetake={() => void resume(pending, false)}
          onUseAnyway={() => void resume(pending, true)}
        />
      ) : null}

      {awaitingCategory !== null ? (
        <CategoryPicker
          fileCount={awaitingCategory.files.length}
          onCancel={cancelCategory}
          onPick={(category) => {
            const held = awaitingCategory;
            setAwaitingCategory(null);
            void accept(held.files, held.source, undefined, category);
          }}
        />
      ) : null}

      <div
        className="capture__drop"
        data-testid="capture-drop"
        data-active={dragging ? "true" : undefined}
        onDragEnter={() => setDragging(true)}
        onDragLeave={(event) => {
          // Only when the pointer leaves the zone itself, not when it crosses
          // onto one of the zone's own children - which would flicker it off.
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          receive([...event.dataTransfer.files], "upload");
        }}
      >
        <span className="capture__drop-icon" aria-hidden="true">
          <Upload size={22} strokeWidth={1.75} />
        </span>
        <p className="capture__drop-hint">{t("capture.screen.drop_hint")}</p>
        <button
          type="button"
          className="capture__choose"
          data-testid="capture-choose-files"
          onClick={() => fileInput.current?.click()}
        >
          {t("capture.screen.choose_files")}
        </button>
      </div>

      {/*
        `capture="environment"` is what makes this the camera on a phone: it
        opens the rear camera directly rather than a gallery. On a desktop it
        degrades to an ordinary file picker, which is the right fallback — the
        two paths land in the same place either way.
      */}
      <input
        ref={cameraInput}
        type="file"
        accept="image/*"
        capture="environment"
        hidden
        data-testid="capture-camera-input"
        onChange={(event) => onFiles(event, "camera")}
      />
      <input
        ref={fileInput}
        type="file"
        accept="image/jpeg,image/png,image/heic,application/pdf"
        multiple
        hidden
        data-testid="capture-file-input"
        onChange={(event) => onFiles(event, "upload")}
      />

      {/*
        The one tap FR-EXP-001 asks for. Largest, most prominent element on the
        screen, anchored near the bottom of the viewport for one-handed thumb
        reach (ADR-047) — present and tappable from the first render, which is
        what `useSitting`'s race fix (see CaptureScreen's own module docstring)
        makes safe rather than lossy.
      */}
      <button
        type="button"
        className="capture__shutter"
        data-testid="capture-take-photo"
        onClick={() => cameraInput.current?.click()}
      >
        <Camera size={22} strokeWidth={1.75} aria-hidden="true" />
        {t("capture.screen.take_photo")}
      </button>

      {/*
        Offered only once there is a receipt to add to. Before that it would be
        a button that either does nothing or quietly starts a receipt, and the
        second is the failure this pair exists to prevent.
      */}
      {sitting.currentReceiptRef !== null ? (
        <div className="capture__toggle">
          <button
            type="button"
            className="capture__toggle-option"
            aria-pressed={joining}
            data-testid="capture-add-page"
            onClick={() => setJoining(true)}
          >
            {t("capture.screen.add_page")}
          </button>
          <button
            type="button"
            className="capture__toggle-option"
            aria-pressed={!joining}
            data-testid="capture-new-receipt"
            onClick={() => setJoining(false)}
          >
            {t("capture.screen.new_receipt")}
          </button>
        </div>
      ) : null}

      <UploadsOverview sitting={sitting} queue={queue} />

      <button
        type="button"
        className="capture__finish"
        disabled={sitting.receipts.length === 0}
        data-testid="capture-finalise"
        onClick={() => void sitting.finalise()}
      >
        {t("capture.screen.finalise")}
      </button>
    </section>
  );
}

function QualityPrompt({
  report,
  onRetake,
  onUseAnyway,
}: {
  report: QualityReport;
  onRetake: () => void;
  onUseAnyway: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);

  // WCAG 2.2 SC 2.4.3. `role="alertdialog"` promises an interruption that
  // requires a decision (FR-EXP-001's retake/use-anyway) before continuing;
  // without this, focus never actually MOVED here and Tab could still reach
  // the shutter and choose-files buttons behind it while the decision this
  // prompt exists to force was still open. `open` is always `true` here —
  // the parent unmounts this component entirely rather than hiding it, which
  // is what makes useModalFocus's unmount-time cleanup the moment focus is
  // restored to whatever was focused before the prompt appeared.
  useModalFocus(true, dialogRef);

  return (
    <div
      ref={dialogRef}
      role="alertdialog"
      aria-modal="true"
      aria-label={t("capture.quality.title")}
      data-testid="capture-quality"
    >
      <p>{t("capture.quality.title")}</p>
      <ul>
        {report.findings.map((finding) => (
          <li key={finding} data-testid="capture-quality-finding" data-finding={finding}>
            {t(`capture.quality.${finding}`)}
          </li>
        ))}
      </ul>
      {/* Retake first, and it is the one the requirement names. */}
      <button type="button" data-testid="capture-quality-retake" onClick={onRetake}>
        {t("capture.quality.retake")}
      </button>
      <button type="button" data-testid="capture-quality-use" onClick={onUseAnyway}>
        {t("capture.quality.use_anyway")}
      </button>
    </div>
  );
}

function SittingProblemNotice({ sitting }: { sitting: Sitting }) {
  const { t } = useI18n();
  if (sitting.problem === null) return null;

  const { reason, detail } = sitting.problem;
  // The cap's two sentences already exist for the queue panel; a second pair
  // saying the same thing differently would be two places to keep in step.
  const key =
    reason === "queue_full"
      ? "capture.queue.full"
      : reason === "queue_file_too_large"
        ? "capture.queue.too_large"
        : `capture.problem.${reason}`;

  return (
    <p role="alert" data-testid="capture-problem" data-reason={reason}>
      {reason === "refused" ? t(key, { detail: detail ?? "" }) : t(key)}
    </p>
  );
}

/**
 * A capture stopped for a warning, with everything still to come behind it.
 *
 * `rest` is why this is not just the flagged file: answering the prompt has to
 * carry on with the batch, and a drop of five files must not become a drop of
 * one because the second one was blurred.
 */
interface PendingCapture {
  readonly input: CaptureInput;
  readonly quality: QualityReport;
  readonly rest: readonly File[];
  readonly source: CaptureSource;
  readonly into: string | undefined;
  readonly category: ExpenseCategoryKey | undefined;
}

/** Files that have been chosen and are waiting on their category. */
interface AwaitingCategory {
  readonly files: readonly File[];
  readonly source: CaptureSource;
}
