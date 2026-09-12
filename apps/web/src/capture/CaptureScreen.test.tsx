import {
  act,
  fireEvent,
  render as renderBare,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { CaptureApi, OfflineError } from "./api";
import { CaptureScreen } from "./CaptureScreen";
import type { DecodeFile } from "./decode";
import { useSitting, type Sitting, type SittingContext } from "./useSitting";
import { MemoryKeyVault, WebCryptoCipher } from "./webCryptoCipher";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

class MemoryStore implements QueueStore {
  readonly records = new Map<string, StoredCapture>();
  async put(record: StoredCapture) {
    this.records.set(record.id, record);
  }
  async all() {
    return [...this.records.values()];
  }
  async remove(id: string) {
    this.records.delete(id);
  }
  async clear() {
    this.records.clear();
  }
}

const context: SittingContext = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
};

/** A decode that reports whatever quality the test wants, with no canvas. */
function decoder(findings: ("blurred" | "glare")[] = []): DecodeFile {
  return async (file: File) => ({
    bytes: new Uint8Array([1, 2, 3]),
    contentType: file.type || "image/jpeg",
    quality: findings.length === 0 ? null : { findings, sharpness: 10, glare: 0.5 },
  });
}

function build({
  openSession = vi.fn(async () => ({
    id: "sess-1",
    open: true,
    opened_at: null,
    finalised_at: null,
    items: [],
    expenses_to_create: 0,
  })),
  finaliseSession = vi.fn(async () => ({})),
  decode = decoder(),
}: {
  openSession?: () => Promise<unknown>;
  finaliseSession?: () => Promise<unknown>;
  decode?: DecodeFile;
} = {}) {
  const store = new MemoryStore();
  const queue = new CaptureQueue({
    store,
    cipher: new WebCryptoCipher(new MemoryKeyVault()),
    clock: { now: () => Date.parse("2026-09-09T10:00:00.000Z") },
    newId: () => `q-${store.records.size}`,
  });
  const api = { openSession, finaliseSession } as unknown as CaptureApi;

  let refs = 0;
  function Harness() {
    const sitting = useSitting({ context, queue, api, newRef: () => `ref-${++refs}` });
    return <CaptureScreen sitting={sitting} queue={queue} decode={decode} />;
  }

  return { store, queue, api, openSession, finaliseSession, Harness };
}

const jpeg = () => new File([new Uint8Array([1, 2, 3])], "bon.jpg", { type: "image/jpeg" });

/**
 * Renders the harness and waits for the auto-started sitting to be ready to
 * capture into — the equivalent of the old `startSitting`, minus the "Start"
 * tap ADR-047 removed. Nothing here taps anything: the sitting starts itself
 * on mount (CaptureScreen's own auto-start effect), and this only waits out
 * that startup.
 */
async function startSitting() {
  const built = build();
  render(<built.Harness />);
  await waitFor(() => screen.getByTestId("capture-drop"));
  return built;
}

describe("starting a sitting — FR-EXP-001a", () => {
  it("opens a session automatically on mount, with no tap", async () => {
    // ADR-047: the "Start" tap is gone. Mounting the screen is enough.
    const { openSession } = await startSitting();

    expect(openSession).toHaveBeenCalledWith("adm-A");
  });

  it("says plainly that a new batch needs a connection", async () => {
    // The one thing a sitting genuinely cannot do offline — the session id is
    // the server's to allocate. Everything after it works without one, and the
    // sentence says so rather than leaving a dead button.
    const built = build({
      openSession: vi.fn(async () => {
        throw new OfflineError("no connection");
      }),
    });
    render(<built.Harness />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-problem").getAttribute("data-reason")).toBe(
        "offline_cannot_start",
      ),
    );
    expect(screen.getByTestId("capture-problem").textContent).toContain("worden gewoon geüpload");
  });
});

/** A `Sitting` this suite fully controls, for asserting on the SCREEN alone — what it renders and
 * calls — independent of `useSitting`'s own behaviour, which the race-condition suite below tests
 * directly instead. */
function fakeSitting(overrides: Partial<Sitting> = {}): Sitting {
  return {
    phase: "idle",
    sessionId: null,
    receipts: [],
    currentReceiptRef: null,
    problem: null,
    start: vi.fn(async () => {}),
    capture: vi.fn(async () => null),
    finalise: vi.fn(async () => false),
    dismissProblem: vi.fn(),
    ...overrides,
  };
}

describe("the one tap FR-EXP-001 asks for — ADR-047", () => {
  it("starts the sitting itself on mount, with no button to tap first", () => {
    const { queue } = build();
    const sitting = fakeSitting({ phase: "idle" });

    render(<CaptureScreen sitting={sitting} queue={queue} decode={decoder()} />);

    expect(sitting.start).toHaveBeenCalledTimes(1);
  });

  it("no longer offers a manual 'Start' button", () => {
    const { queue } = build();
    const sitting = fakeSitting({ phase: "idle" });

    render(<CaptureScreen sitting={sitting} queue={queue} decode={decoder()} />);

    expect(screen.queryByTestId("capture-start")).toBeNull();
  });

  it("shows the take-photo button immediately, before the session finishes opening", () => {
    // The session is still "opening" — exactly the window a fast tap in a car
    // park would land in. The button must already be there.
    const { queue } = build();
    const sitting = fakeSitting({ phase: "opening" });

    render(<CaptureScreen sitting={sitting} queue={queue} decode={decoder()} />);

    const button = screen.getByTestId("capture-take-photo");
    expect(button).toBeTruthy();
    expect(button.hasAttribute("disabled")).toBe(false);
  });

  it("shows the take-photo button while the sitting is still idle too", () => {
    const { queue } = build();
    const sitting = fakeSitting({ phase: "idle" });

    render(<CaptureScreen sitting={sitting} queue={queue} decode={decoder()} />);

    expect(screen.getByTestId("capture-take-photo")).toBeTruthy();
  });

  it("still surfaces a genuine start failure, gate screen or not", () => {
    const { queue } = build();
    const sitting = fakeSitting({
      phase: "idle",
      problem: { reason: "offline_cannot_start" },
    });

    render(<CaptureScreen sitting={sitting} queue={queue} decode={decoder()} />);

    expect(screen.getByTestId("capture-problem").getAttribute("data-reason")).toBe(
      "offline_cannot_start",
    );
    // The shutter is still there — visible but non-functional is fine per the
    // task's own scope; disappearing entirely, or the whole screen being
    // replaced by a dead end, would not be.
    expect(screen.getByTestId("capture-take-photo")).toBeTruthy();
  });

  it("does not call start() a second time across a re-render", () => {
    const { queue } = build();
    const sitting = fakeSitting({ phase: "idle" });

    const { rerender } = render(
      <CaptureScreen sitting={sitting} queue={queue} decode={decoder()} />,
    );
    rerender(
      <I18nProvider initialLanguage="nl">
        <CaptureScreen sitting={{ ...sitting }} queue={queue} decode={decoder()} />
      </I18nProvider>,
    );

    expect(sitting.start).toHaveBeenCalledTimes(1);
  });
});

describe("a capture during an in-flight start — the race ADR-047 fixes", () => {
  // ADR-036 already named the one thing a sitting genuinely cannot do
  // offline: obtaining a session id needs the server. This suite is about a
  // DIFFERENT, narrower failure that only exists because the screen now
  // starts a session on its own instead of waiting for a tap: a capture
  // landing in the gap between `start()` being called and its promise
  // settling must not be read as "no session" and silently dropped.
  it("does not lose a photograph captured before start() has resolved", async () => {
    // The deferred-promise technique this codebase already uses for
    // controlling async timing in a test (see i18n.test.tsx's `resolveFetch`),
    // rather than inventing a new one.
    let resolveOpen: (session: {
      id: string;
      open: boolean;
      opened_at: null;
      finalised_at: null;
      items: never[];
      expenses_to_create: number;
    }) => void = () => {};
    const opening = new Promise<{
      id: string;
      open: boolean;
      opened_at: null;
      finalised_at: null;
      items: never[];
      expenses_to_create: number;
    }>((resolve) => {
      resolveOpen = resolve;
    });
    const openSession = vi.fn(() => opening);
    const store = new MemoryStore();
    const queue = new CaptureQueue({
      store,
      cipher: new WebCryptoCipher(new MemoryKeyVault()),
      clock: { now: () => Date.parse("2026-09-09T10:00:00.000Z") },
      newId: () => `q-${store.records.size}`,
    });
    const api = { openSession, finaliseSession: vi.fn(async () => ({})) } as unknown as CaptureApi;

    const { result } = renderHook(() => useSitting({ context, queue, api }));

    // Fire `start()` — the auto-start effect's own call shape — WITHOUT
    // awaiting it, exactly as CaptureScreen's effect does.
    act(() => {
      void result.current.start();
    });
    expect(result.current.phase).toBe("opening");

    // A tap on the shutter lands here: before `openSession` has answered.
    // Naively this used to read `sessionId === null` and return `null`,
    // dropping the photograph on the floor with no trace of it anywhere.
    let capturePromise!: ReturnType<Sitting["capture"]>;
    act(() => {
      capturePromise = result.current.capture({
        bytes: new Uint8Array([1, 2, 3]),
        contentType: "image/jpeg",
        filename: "bon.jpg",
        source: "camera",
      });
    });

    // Nothing has been dropped YET — the capture is waiting rather than
    // having already resolved to null. Only once the session opens does it
    // get an answer.
    await Promise.resolve();
    expect(store.records.size).toBe(0);

    const captureResult = await act(async () => {
      resolveOpen({
        id: "sess-1",
        open: true,
        opened_at: null,
        finalised_at: null,
        items: [],
        expenses_to_create: 0,
      });
      return capturePromise;
    });

    expect(captureResult?.accepted).toBe(true);
    expect(store.records.size).toBe(1);
    expect(result.current.phase).toBe("capturing");
    expect(result.current.sessionId).toBe("sess-1");
  });

  it("still returns null for a capture made while starting is genuinely, permanently failing", async () => {
    // The constraint ADR-036 already names and this task deliberately leaves
    // alone: truly offline, there is no session id to be had, ever. Waiting
    // out the in-flight `start()` must not turn into waiting forever, and it
    // must not manufacture a session id that was never granted.
    const store = new MemoryStore();
    const queue = new CaptureQueue({
      store,
      cipher: new WebCryptoCipher(new MemoryKeyVault()),
      clock: { now: () => Date.parse("2026-09-09T10:00:00.000Z") },
      newId: () => `q-${store.records.size}`,
    });
    const openSession = vi.fn(async () => {
      throw new OfflineError("no connection");
    });
    const api = { openSession, finaliseSession: vi.fn(async () => ({})) } as unknown as CaptureApi;

    const { result } = renderHook(() => useSitting({ context, queue, api }));

    act(() => {
      void result.current.start();
    });
    expect(result.current.phase).toBe("opening");

    // Same shape as the successful race above: a capture lands while `start`
    // is still in flight, and `capture()` waits for it rather than reading
    // the not-yet-settled session as an immediate loss.
    let capturePromise!: ReturnType<Sitting["capture"]>;
    act(() => {
      capturePromise = result.current.capture({
        bytes: new Uint8Array([1, 2, 3]),
        contentType: "image/jpeg",
        filename: "bon.jpg",
        source: "camera",
      });
    });

    const captureResult = await act(async () => capturePromise);

    // `start()` never actually rejects — it catches its own failure and
    // records `problem` — so this is the ONLY way `capture()` can still end
    // in `null` after this task's fix: the wait genuinely came back with no
    // session id, exactly the ADR-036 constraint this task leaves untouched.
    expect(captureResult).toBeNull();
    expect(store.records.size).toBe(0);
    expect(result.current.problem?.reason).toBe("offline_cannot_start");
    expect(result.current.phase).toBe("idle");
  });
});

describe("both paths land in the same place — FR-EXP-001", () => {
  it("queues a photograph from the camera", async () => {
    const { store } = await startSitting();

    fireEvent.change(screen.getByTestId("capture-camera-input"), {
      target: { files: [jpeg()] },
    });

    await waitFor(() => expect(store.records.size).toBe(1));
  });

  it("queues a file from the picker", async () => {
    const { store } = await startSitting();

    fireEvent.change(screen.getByTestId("capture-file-input"), {
      target: { files: [jpeg()] },
    });

    await waitFor(() => expect(store.records.size).toBe(1));
  });

  it("queues a file that was dropped", async () => {
    const { store } = await startSitting();

    fireEvent.drop(screen.getByTestId("capture-drop"), {
      dataTransfer: { files: [jpeg()] },
    });

    await waitFor(() => expect(store.records.size).toBe(1));
  });

  it("queues rather than uploading, so capture works with no connection", async () => {
    // ADR-035 §2: there is no online fast path. The screen never posts a page
    // itself, which is why nothing here needs a transport.
    const { store } = await startSitting();

    fireEvent.change(screen.getByTestId("capture-file-input"), {
      target: { files: [jpeg()] },
    });

    await waitFor(() => expect(store.records.size).toBe(1));
    const [record] = [...store.records.values()];
    expect(record?.state).toBe("queued");
  });
});

describe("the two 'several' — FR-EXP-001 multi-page vs FR-EXP-001a batch", () => {
  it("makes each photograph a separate receipt by default", async () => {
    const { store } = await startSitting();

    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(store.records.size).toBe(1));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(store.records.size).toBe(2));

    const refs = [...store.records.values()].map((record) => record.receiptRef);
    expect(new Set(refs).size).toBe(2);
    expect(screen.getByTestId("capture-expenses-to-create").textContent).toContain("2 uitgaven");
  });

  it("joins the next photograph to the same receipt when asked to", async () => {
    // The three-page-invoice case. Getting this the wrong way round claims it
    // three times, and the mistake is silent — which is why it is a button.
    const { store } = await startSitting();
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => screen.getByTestId("capture-add-page"));

    fireEvent.click(screen.getByTestId("capture-add-page"));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });

    await waitFor(() => expect(store.records.size).toBe(2));
    const refs = [...store.records.values()].map((record) => record.receiptRef);
    expect(new Set(refs).size).toBe(1);
    expect([...store.records.values()].map((record) => record.pageIndex).sort()).toEqual([0, 1]);
  });

  it("counts a multi-page receipt as ONE expense", async () => {
    const { store } = await startSitting();
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => screen.getByTestId("capture-add-page"));
    fireEvent.click(screen.getByTestId("capture-add-page"));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(store.records.size).toBe(2));

    expect(screen.getByTestId("capture-expenses-to-create").textContent).toContain("1 uitgave");
    expect(screen.getByTestId("capture-review-pages").textContent).toContain("2 pagina's");
  });

  it("goes back to starting new receipts after one page has been added", async () => {
    // "Another page" is armed for one capture, not until it is turned off —
    // the sticky version is how somebody ends up with a six-page receipt they
    // meant to be six claims.
    const { store } = await startSitting();
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => screen.getByTestId("capture-add-page"));
    fireEvent.click(screen.getByTestId("capture-add-page"));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(store.records.size).toBe(2));

    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });

    await waitFor(() => expect(store.records.size).toBe(3));
    const refs = [...store.records.values()].map((record) => record.receiptRef);
    expect(new Set(refs).size).toBe(2);
  });

  it("offers no 'add a page' before anything has been captured", async () => {
    await startSitting();

    expect(screen.queryByTestId("capture-add-page")).toBeNull();
  });
});

describe("glare and blur warnings — FR-EXP-001", () => {
  it("prompts for a retake instead of queueing a bad photograph", async () => {
    const built = build({ decode: decoder(["blurred"]) });
    render(<built.Harness />);
    await waitFor(() => screen.getByTestId("capture-drop"));

    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });

    await waitFor(() => screen.getByTestId("capture-quality"));
    expect(screen.getByTestId("capture-quality-finding").getAttribute("data-finding")).toBe(
      "blurred",
    );
    expect(built.store.records.size).toBe(0);
  });

  it("queues nothing when the person retakes", async () => {
    const built = build({ decode: decoder(["glare"]) });
    render(<built.Harness />);
    await waitFor(() => screen.getByTestId("capture-drop"));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => screen.getByTestId("capture-quality"));

    fireEvent.click(screen.getByTestId("capture-quality-retake"));

    await waitFor(() => expect(screen.queryByTestId("capture-quality")).toBeNull());
    expect(built.store.records.size).toBe(0);
  });

  it("always lets the person keep the photograph anyway", async () => {
    // These warn and never refuse. A blurred photograph of a receipt is worth
    // more than no photograph of it, and the checks are heuristics.
    const built = build({ decode: decoder(["blurred", "glare"]) });
    render(<built.Harness />);
    await waitFor(() => screen.getByTestId("capture-drop"));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => screen.getByTestId("capture-quality"));

    fireEvent.click(screen.getByTestId("capture-quality-use"));

    await waitFor(() => expect(built.store.records.size).toBe(1));
  });
});

describe("several files at once", () => {
  const files = (count: number) =>
    Array.from(
      { length: count },
      (_unused, index) =>
        new File([new Uint8Array([index])], `bon-${index}.jpg`, { type: "image/jpeg" }),
    );

  it("makes a separate receipt of each dropped file", async () => {
    const { store } = await startSitting();

    fireEvent.drop(screen.getByTestId("capture-drop"), { dataTransfer: { files: files(3) } });

    await waitFor(() => expect(store.records.size).toBe(3));
    const refs = [...store.records.values()].map((record) => record.receiptRef);
    expect(new Set(refs).size).toBe(3);
  });

  it("numbers the pages of a scanned multi-page document 0, 1, 2, 3", async () => {
    // React batches state updates, so a page number derived from rendered
    // state would make all four of these page zero — and page zero is the
    // number that opens a receipt. Four page zeroes is four claims.
    const { store } = await startSitting();
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: files(1) } });
    await waitFor(() => screen.getByTestId("capture-add-page"));
    fireEvent.click(screen.getByTestId("capture-add-page"));

    fireEvent.drop(screen.getByTestId("capture-drop"), { dataTransfer: { files: files(3) } });

    await waitFor(() => expect(store.records.size).toBe(4));
    const pages = [...store.records.values()].map((record) => record.pageIndex).sort();
    expect(pages).toEqual([0, 1, 2, 3]);
    expect(new Set([...store.records.values()].map((r) => r.receiptRef)).size).toBe(1);
  });

  it("carries on with the rest of a batch after a warning is answered", async () => {
    // A drop of three where the first is blurred must not become a drop of
    // one. The remaining files are held behind the prompt, not discarded.
    const built = build({ decode: decoder(["blurred"]) });
    render(<built.Harness />);
    await waitFor(() => screen.getByTestId("capture-drop"));

    fireEvent.drop(screen.getByTestId("capture-drop"), { dataTransfer: { files: files(3) } });
    await waitFor(() => screen.getByTestId("capture-quality"));
    fireEvent.click(screen.getByTestId("capture-quality-use"));

    // Each remaining file is flagged too, so the prompt reappears; answering
    // it each time must walk the whole batch.
    await waitFor(() => screen.getByTestId("capture-quality"));
    fireEvent.click(screen.getByTestId("capture-quality-use"));
    await waitFor(() => screen.getByTestId("capture-quality"));
    fireEvent.click(screen.getByTestId("capture-quality-use"));

    await waitFor(() => expect(built.store.records.size).toBe(3));
  });

  it("keeps going with the rest when a flagged page is retaken away", async () => {
    const built = build({ decode: decoder(["glare"]) });
    render(<built.Harness />);
    await waitFor(() => screen.getByTestId("capture-drop"));

    fireEvent.drop(screen.getByTestId("capture-drop"), { dataTransfer: { files: files(2) } });
    await waitFor(() => screen.getByTestId("capture-quality"));
    fireEvent.click(screen.getByTestId("capture-quality-retake"));

    await waitFor(() => screen.getByTestId("capture-quality"));
    fireEvent.click(screen.getByTestId("capture-quality-use"));

    await waitFor(() => expect(built.store.records.size).toBe(1));
  });
});

describe("finishing a sitting — FR-EXP-001a", () => {
  it("cannot be finished before anything is captured", async () => {
    await startSitting();

    expect(screen.getByTestId("capture-finalise").hasAttribute("disabled")).toBe(true);
  });

  it("accepts the review list and says nothing has been posted", async () => {
    // ADR-031 §5: finalisation closes the SESSION. Nothing reaches the ledger,
    // and the screen must not imply that it has.
    const { store, finaliseSession } = await startSitting();
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(store.records.size).toBe(1));

    fireEvent.click(screen.getByTestId("capture-finalise"));

    await waitFor(() => screen.getByTestId("capture-finalised"));
    expect(finaliseSession).toHaveBeenCalledWith("adm-A", "sess-1");
    expect(screen.getByTestId("capture-finalised").textContent).toContain("nog niets geboekt");
  });

  it("explains that pages still have to upload before a batch can be finished", async () => {
    const refusal = Object.assign(new Error("not yet"), {
      reason: "capture_item_without_evidence",
    });
    const built = build({
      finaliseSession: vi.fn(async () => {
        throw refusal;
      }),
    });
    render(<built.Harness />);
    await waitFor(() => screen.getByTestId("capture-drop"));
    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(built.store.records.size).toBe(1));

    fireEvent.click(screen.getByTestId("capture-finalise"));

    await waitFor(() =>
      expect(screen.getByTestId("capture-problem").getAttribute("data-reason")).toBe(
        "pages_not_uploaded",
      ),
    );
  });
});

describe("tenant context — CLAUDE.md rule 1, client side", () => {
  it("seals the administration the sitting was opened for into every capture", async () => {
    const { store, queue } = await startSitting();

    fireEvent.change(screen.getByTestId("capture-file-input"), { target: { files: [jpeg()] } });
    await waitFor(() => expect(store.records.size).toBe(1));

    const [record] = [...store.records.values()];
    const { payload } = await queue.open(record!);
    expect(payload.administrationId).toBe("adm-A");
    expect(payload.sessionId).toBe("sess-1");
    expect(payload.fiscalYearId).toBe("fy-2026");
  });
});
