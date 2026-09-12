/**
 * One sitting — FR-EXP-001, FR-EXP-001a, FR-EXP-001f.
 *
 *     FR-EXP-001a  Batch capture: photograph or upload several receipts in one
 *                  session, each becoming a separate expense, with a review
 *                  list before posting.
 *
 * The state between "the screen opened" and "the review list was accepted".
 * Everything it holds is about ONE administration, given once and never
 * re-read from anywhere ambient — the same rule the queue follows, for the
 * same reason (CLAUDE.md's first rule, client side).
 *
 * --- The two "several", kept apart here as well as in the schema ---
 *
 * ADR-031 gave the schema three levels so that multi-page and batch could not
 * be confused. This hook is where a person's intention picks between them, and
 * it is a deliberate act either way:
 *
 *     capture()                  a NEW receipt   → a new expense
 *     capture({ into: ref })     ANOTHER PAGE    → no new expense
 *
 * Nothing is inferred from timing or similarity. The screen has two buttons,
 * and the difference between them is one expense or three.
 *
 * --- The sitting does not survive a reload, and nothing is lost when it does ---
 *
 * `sessionId` and the local receipt list live in React state. A reload
 * mid-sitting loses the LIST; it loses no receipts, because every page is
 * already in the offline queue with its own tenant context sealed alongside it,
 * and the uploader will deliver them whether or not this hook still exists.
 * What the person does after a reload is start a new sitting, and read the old
 * one back from the server once its pages have landed.
 *
 * Persisting it would mean writing an administration id to unencrypted device
 * storage, which is exactly what ADR-035 §5 sealed the queue's own metadata to
 * avoid — a poor trade for recovering a list.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import type { CaptureQueue, CaptureSource, EnqueueResult } from "@ledgr/offline-queue";

import { OfflineError, type CaptureApi } from "./api";

/**
 * Where a sitting posts. Supplied by whatever knows the session — today a
 * prop, because there is no sign-in flow and no active-client endpoint wired
 * (see App.tsx and auth/SignInPending), and it stays a prop when there is: a
 * hook that reached for the "currently open client" itself is the one that
 * uploads a receipt into the wrong one.
 */
export interface SittingContext {
  readonly organizationId: string;
  readonly administrationId: string;
  readonly fiscalYearId: string;
  readonly userId: string;
}

/** A receipt captured in this sitting, as this screen knows it. */
export interface LocalReceipt {
  /** The client-local reference the queue groups pages under. */
  readonly ref: string;
  readonly pages: readonly LocalPage[];
}

export interface LocalPage {
  readonly queueId: string;
  readonly pageIndex: number;
  readonly filename: string | null;
  readonly contentType: string;
}

export type SittingPhase =
  /** No session yet. The screen offers to start one. */
  | "idle"
  | "opening"
  /** A session is open and pages can be captured. */
  | "capturing"
  /** The review list was accepted; this sitting is closed. */
  | "finalised";

export interface Sitting {
  readonly phase: SittingPhase;
  readonly sessionId: string | null;
  readonly receipts: readonly LocalReceipt[];
  /**
   * The receipt a further page would join — the last one captured. Null before
   * anything has been photographed, which is when "add a page" makes no sense
   * and the screen does not offer it.
   */
  readonly currentReceiptRef: string | null;
  /** Set when the last action failed in a way the screen should say out loud. */
  readonly problem: SittingProblem | null;
  start(): Promise<void>;
  capture(input: CaptureInput): Promise<EnqueueResult | null>;
  finalise(): Promise<boolean>;
  dismissProblem(): void;
}

/**
 * What went wrong, as something the screen can translate.
 *
 * A `reason` string rather than a sentence: the catalogue owns the words
 * (FR-LOC-001), and a hook that returned prose would be a second place they
 * live.
 */
export interface SittingProblem {
  readonly reason:
    | "offline_cannot_start"
    | "offline_cannot_finalise"
    | "queue_full"
    | "queue_file_too_large"
    | "pages_not_uploaded"
    | "refused";
  /** The API's own already-translated sentence, where there was one. */
  readonly detail?: string;
}

export interface CaptureInput {
  readonly bytes: Uint8Array<ArrayBuffer>;
  readonly contentType: string;
  readonly filename: string | null;
  readonly source: CaptureSource;
  /**
   * Present to add ANOTHER PAGE to a receipt already captured; absent to start
   * a new one. Never inferred — see the module docstring.
   */
  readonly into?: string;
}

export function useSitting({
  context,
  queue,
  api,
  newRef = () => crypto.randomUUID(),
}: {
  context: SittingContext;
  queue: CaptureQueue;
  api: CaptureApi;
  newRef?: () => string;
}): Sitting {
  const [phase, setPhase] = useState<SittingPhase>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<readonly LocalReceipt[]>([]);
  const [currentReceiptRef, setCurrentReceiptRef] = useState<string | null>(null);
  const [problem, setProblem] = useState<SittingProblem | null>(null);

  /**
   * `sessionId`'s value, readable synchronously from inside `capture()`.
   *
   * `capture()` closes over the `sessionId` a render held at the moment it was
   * called — ordinary React staleness, and ordinarily harmless because a fresh
   * `capture` is handed out every render. It stops being harmless the moment
   * `capture()` itself `await`s something (see `startPromise` below): after
   * that `await`, the closed-over `sessionId` is still whatever it was before
   * the wait, not whatever `start()` set it to while waiting was in progress.
   * A ref is written synchronously in the same tick as `setSessionId`, so it
   * carries the up-to-date value into that resumed continuation.
   */
  const sessionIdRef = useRef<string | null>(null);

  /**
   * The in-flight `start()` call, if any — the fix for the race a fast tap
   * makes possible once the screen auto-starts a sitting instead of waiting
   * for one.
   *
   * `capture()` used to see this as "no session yet" and drop the photograph
   * (`if (sessionId === null) return null`). That was correct when a person
   * had to tap "Start" and then wait for it to finish before "Take Photo" ever
   * appeared — nobody could tap a button that was not on the screen. It stops
   * being correct once the screen shows the shutter button immediately and
   * starts the session for them: the two are now in a real race, and a tap
   * that lands during the gap would have its photograph silently discarded.
   * Holding the promise here lets `capture()` wait out that gap instead of
   * guessing that it lost. This does not touch the gap ADR-036 itself records
   * — starting a sitting still needs a connection, and a start that genuinely
   * fails still leaves `capture()` with no session id to hand back.
   */
  const startPromise = useRef<Promise<void> | null>(null);

  /**
   * The next page number for each receipt, kept in a ref rather than derived
   * from `receipts`.
   *
   * State updates are batched, so several files handed over in one drop would
   * all read the same `receipts` and all be numbered as though they were
   * first. A ref is written synchronously, which is what makes the four files
   * of a four-page invoice pages 0, 1, 2 and 3 rather than four page zeroes —
   * and page zero is the number that opens a receipt.
   */
  const nextPage = useRef(new Map<string, number>());

  const start = useCallback(async () => {
    setPhase("opening");
    setProblem(null);

    // The attempt is a plain async function, not `start` itself, so the
    // promise can be stashed in `startPromise` synchronously — before this
    // function's own first `await` — and be there for a `capture()` called in
    // the same tick as `start()`, immediately after, with neither awaited.
    const attempt = (async () => {
      try {
        const session = await api.openSession(context.administrationId);
        sessionIdRef.current = session.id;
        setSessionId(session.id);
        setPhase("capturing");
      } catch (error) {
        setPhase("idle");
        // The one place FR-EXP-001f is not yet whole: a sitting needs a
        // session id, the server allocates it, and there is no way to ask for
        // one without a connection. Everything AFTER this point works
        // offline. See ADR-036's known gaps.
        setProblem({
          reason: error instanceof OfflineError ? "offline_cannot_start" : "refused",
          detail: error instanceof Error ? error.message : undefined,
        });
      }
    })();

    startPromise.current = attempt;
    try {
      await attempt;
    } finally {
      // Only clear it if it is still THIS attempt: a second `start()` called
      // before the first settles (not a path the screen offers today, but not
      // this ref's job to rule out) would otherwise have its own in-flight
      // promise erased by the first one finishing.
      if (startPromise.current === attempt) startPromise.current = null;
    }
  }, [api, context.administrationId]);

  const capture = useCallback(
    async (input: CaptureInput): Promise<EnqueueResult | null> => {
      // The race this exists for: the screen now starts a sitting on its own,
      // in the background, and shows the shutter button before that finishes.
      // A tap landing in the gap must not read as "no session" and vanish —
      // it waits for the session that is already on its way.
      if (phase === "opening" && startPromise.current !== null) {
        try {
          await startPromise.current;
        } catch {
          // `start()` never actually throws past itself — it catches its own
          // failure and records it as `problem` — but awaiting someone else's
          // promise is awaiting someone else's promise. Swallowed either way:
          // what matters below is whether a session id came out of it, not
          // how the wait ended.
        }
      }

      // Read fresh, not the `sessionId` this closure was called with: after
      // the `await` above, that closed-over value is exactly what it was
      // before waiting, while `sessionIdRef` was written synchronously by
      // whichever `start()` attempt just resolved.
      const activeSessionId = sessionIdRef.current;
      if (activeSessionId === null) return null;
      setProblem(null);

      const ref = input.into ?? newRef();
      const pageIndex = nextPage.current.get(ref) ?? 0;
      nextPage.current.set(ref, pageIndex + 1);

      const result = await queue.enqueue({
        ...context,
        sessionId: activeSessionId,
        receiptRef: ref,
        pageIndex,
        source: input.source,
        filename: input.filename,
        contentType: input.contentType,
        image: input.bytes,
      });

      if (!result.accepted) {
        // MOB-009's cap. The queue refuses the NEW capture rather than
        // evicting a queued one, so nothing has been lost — but the person has
        // to be told now, while they are still holding the receipt.
        //
        // The page number is given back, so a refused page 2 does not leave a
        // hole that makes the next one page 3 of a two-page receipt.
        nextPage.current.set(ref, pageIndex);
        setProblem({
          reason: result.reason === "cap_exceeded" ? "queue_full" : "queue_file_too_large",
        });
        return result;
      }

      const page: LocalPage = {
        queueId: result.id,
        pageIndex,
        filename: input.filename,
        contentType: input.contentType,
      };
      setReceipts((current) =>
        current.some((receipt) => receipt.ref === ref)
          ? current.map((receipt) =>
              receipt.ref === ref ? { ...receipt, pages: [...receipt.pages, page] } : receipt,
            )
          : [...current, { ref, pages: [page] }],
      );
      setCurrentReceiptRef(ref);
      return result;
    },
    [context, newRef, phase, queue],
  );

  const finalise = useCallback(async () => {
    if (sessionId === null) return false;
    setProblem(null);
    try {
      await api.finaliseSession(context.administrationId, sessionId);
      setPhase("finalised");
      return true;
    } catch (error) {
      if (error instanceof OfflineError) {
        setProblem({ reason: "offline_cannot_finalise" });
        return false;
      }
      // The server refuses a session holding an item with no stored original
      // (ADR-031 §5) — which, from a client with a queue, means "your pages
      // have not uploaded yet". That is a different sentence from a generic
      // refusal, and it is the common one.
      const reason =
        error instanceof Error &&
        "reason" in error &&
        error.reason === "capture_item_without_evidence"
          ? "pages_not_uploaded"
          : "refused";
      setProblem({ reason, detail: error instanceof Error ? error.message : undefined });
      return false;
    }
  }, [api, context.administrationId, sessionId]);

  const dismissProblem = useCallback(() => setProblem(null), []);

  return useMemo(
    () => ({
      phase,
      sessionId,
      receipts,
      currentReceiptRef,
      problem,
      start,
      capture,
      finalise,
      dismissProblem,
    }),
    [
      phase,
      sessionId,
      receipts,
      currentReceiptRef,
      problem,
      start,
      capture,
      finalise,
      dismissProblem,
    ],
  );
}

/**
 * How many expenses finalising this sitting would create — FR-EXP-001a's
 * "each becoming a separate expense", answered before anybody commits.
 *
 * One per RECEIPT. Pages do not count, and that is the whole distinction the
 * screen exists to keep straight.
 */
export function expensesThisSittingWouldCreate(receipts: readonly LocalReceipt[]): number {
  return receipts.length;
}
