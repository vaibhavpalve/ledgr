import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { MemoryKeyVault, WebCryptoCipher } from "./capture/webCryptoCipher";
import { jsonResponse } from "./testing/fakeFetch";
import { renderApp } from "./testing/renderApp";
import { meFixture } from "./testing/session";

/**
 * The composition root, as it is since ADR-058: a router, an `AuthProvider`
 * holding who is signed in, and an `AuthenticatedLayout` that loads
 * `GET /v1/me` before any screen behind it renders.
 *
 * What this file asserts is the WIRING between those — that a sign-in lands
 * where it should, that the MFA gate has no way past it, that a Google
 * redirect is recognised, that signing out returns to `/login`, and that
 * MOB-009's purge warning still sits in front of it. Each screen's own
 * behaviour is tested in its own suite.
 */

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  capturesAtRiskMock.mockClear();
  capturesAtRiskMock.mockResolvedValue(0);
  purgeCaptureQueueMock.mockClear();
});

class MemoryStore implements QueueStore {
  private readonly records = new Map<string, StoredCapture>();
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

// The real composition (capture/queue.ts) opens an IndexedDB database, which
// jsdom does not have — the same reason every other suite touching capture
// builds its own `CaptureQueue` over a `MemoryStore`. `capturesAtRisk` and
// `purgeCaptureQueue` are MOB-009's sign-out wiring, mocked here so this file
// can assert that `AuthProvider` still asks before it signs out.
//
// `vi.hoisted` because `vi.mock`'s factory runs before this file's own
// top-level code, so a plain `const` would not exist yet when it closes over
// it.
const { capturesAtRiskMock, purgeCaptureQueueMock, startUploadsMock } = vi.hoisted(() => ({
  capturesAtRiskMock: vi.fn(async () => 0),
  purgeCaptureQueueMock: vi.fn(async (reason: string) => ({ reason, discarded: 0 })),
  // A `QueueUploader`, not a teardown function: `SessionProvider` starts one
  // for as long as a session is ready (MOB-003) and calls `.stop()` on unmount.
  startUploadsMock: vi.fn(() => ({ stop: () => {} })),
}));

vi.mock("./capture/queue", () => ({
  captureQueue: () =>
    new CaptureQueue({
      store: new MemoryStore(),
      cipher: new WebCryptoCipher(new MemoryKeyVault()),
      clock: { now: () => Date.now() },
      newId: () => crypto.randomUUID(),
    }),
  capturesAtRisk: capturesAtRiskMock,
  purgeCaptureQueue: purgeCaptureQueueMock,
  startCaptureUploads: startUploadsMock,
}));

const authResult = (mfaVerified: boolean) =>
  jsonResponse({
    access_token: "tok",
    token_type: "bearer",
    mfa_verified: mfaVerified,
    ...(mfaVerified ? {} : { mfa: { has_passkey: false, has_totp: false } }),
  });

/**
 * A `fetch` routed by `METHOD url`, answering the two reads every
 * authenticated render makes — `GET /v1/me` (the session bootstrap) and
 * `GET /v1/me/language` (IAM-010g's first-login effect) — so that a test only
 * has to state the call it is actually about.
 */
function routedFetch(handlers: Record<string, () => Response>): typeof fetch {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    const handler = handlers[key];
    if (handler) return handler();
    if (key === "GET /v1/me") return jsonResponse(meFixture());
    if (key === "GET /v1/me/language") {
      return jsonResponse({ language: null, supported: ["en", "nl"] });
    }
    return new Response(null, { status: 500 });
  }) as unknown as typeof fetch;
}

/** The dashboard is the landing screen; it is signed-in-ness we are asserting, not its contents. */
const signedIn = () => screen.findByTestId("app-shell");

describe("App — the unauthenticated door", () => {
  it("renders the login screen at /login", () => {
    renderApp({}, { route: "/login" });
    // Two, not one: PreAuthScreen (ADR-057) renders a wordmark in the task
    // panel AND in the marketing rail, toggled by viewport width via CSS
    // rather than JS — jsdom does not evaluate that media query, so both are
    // genuinely in the DOM regardless of which a real browser would show.
    expect(screen.getAllByText("LEDGR")).toHaveLength(2);
    expect(screen.getByTestId("login-form")).toBeDefined();
  });

  it("sends an anonymous visitor from a protected URL to /login", async () => {
    renderApp({}, { route: "/invoices" });

    await waitFor(() => expect(screen.getByTestId("login-form")).toBeDefined());
    expect(screen.queryByTestId("app-shell")).toBeNull();
  });

  it("switching to signup and back keeps the same frame", async () => {
    vi.stubGlobal("fetch", routedFetch({}));
    renderApp({}, { route: "/login" });

    fireEvent.click(screen.getByTestId("switch-to-signup"));
    await waitFor(() => expect(screen.getByTestId("signup-form")).toBeDefined());

    fireEvent.click(screen.getByTestId("switch-to-login"));
    await waitFor(() => expect(screen.getByTestId("login-form")).toBeDefined());
  });
});

describe("ADR-054: signup/login/MFA wired end to end", () => {
  it("a password login that has not cleared MFA lands on the enrolment gate, never the app", async () => {
    vi.stubGlobal("fetch", routedFetch({ "POST /v1/auth/login": () => authResult(false) }));

    renderApp({ language: "nl" }, { route: "/login" });
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() => expect(screen.getByTestId("mfa-enrollment")).toBeDefined());
    expect(screen.queryByTestId("app-shell")).toBeNull();
  });

  it("a session pending MFA cannot reach a protected URL by typing it", async () => {
    // IAM-011 has no opt-out, and the router must not become one: the guard
    // sends a pending session to /mfa from wherever it was going.
    vi.stubGlobal("fetch", routedFetch({ "POST /v1/auth/login": () => authResult(false) }));

    renderApp({ language: "nl" }, { route: "/login" });
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));
    await waitFor(() => expect(screen.getByTestId("mfa-enrollment")).toBeDefined());

    expect(screen.queryByTestId("app-shell")).toBeNull();
  });

  it("completing TOTP enrolment on the gate moves into the authenticated shell", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        "POST /v1/auth/login": () => authResult(false),
        "POST /v1/auth/mfa/totp/enroll/begin": () =>
          jsonResponse({ secret: "JBSWY3DPEHPK3PXP", provisioning_uri: "otpauth://x" }),
        "POST /v1/auth/mfa/totp/enroll/confirm": () => authResult(true),
      }),
    );

    renderApp({ language: "nl" }, { route: "/login" });
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() => expect(screen.getByTestId("mfa-totp-begin")).toBeDefined());
    fireEvent.click(screen.getByTestId("mfa-totp-begin"));
    await waitFor(() => expect(screen.getByTestId("mfa-totp-code")).toBeDefined());
    fireEvent.change(screen.getByTestId("mfa-totp-code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByTestId("mfa-totp-confirm"));

    expect(await signedIn()).toBeDefined();
    expect(screen.queryByTestId("mfa-enrollment")).toBeNull();
  });

  it("a passkey sign-in goes straight in, because IAM-012 counts it as both factors", async () => {
    vi.stubGlobal("fetch", routedFetch({ "POST /v1/auth/login": () => authResult(true) }));

    renderApp({ language: "nl" }, { route: "/login" });
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    expect(await signedIn()).toBeDefined();
  });

  it("a Google OAuth redirect landing shows the callback screen, not the login form", async () => {
    window.history.pushState({}, "", "/?code=auth-code&state=state-value");
    const fetchStub = routedFetch({
      "POST /v1/auth/login/google/callback": () => authResult(true),
    });
    vi.stubGlobal("fetch", fetchStub);

    renderApp({ language: "nl" }, { route: "/?code=auth-code&state=state-value" });

    expect(screen.queryByTestId("login-form")).toBeNull();
    expect(await signedIn()).toBeDefined();
    expect(fetchStub).toHaveBeenCalled();
  });
});

describe("Signing out", () => {
  async function signInAndOut(handlers: Record<string, () => Response> = {}) {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        "POST /v1/auth/login": () => authResult(true),
        "POST /v1/auth/logout": () => jsonResponse({ status: "logged_out" }),
        ...handlers,
      }),
    );

    renderApp({ language: "nl" }, { route: "/login" });
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));
    await signedIn();

    fireEvent.click(screen.getByTestId("user-menu-trigger"));
    fireEvent.click(await screen.findByTestId("sign-out"));
  }

  it("returns to the login screen", async () => {
    await signInAndOut();
    await waitFor(() => expect(screen.getByTestId("login-form")).toBeDefined());
  });

  it("MOB-009: warns while captures are queued, and cancelling keeps the session", async () => {
    capturesAtRiskMock.mockResolvedValueOnce(3);
    await signInAndOut();

    await waitFor(() => expect(screen.getByTestId("sign-out-confirm")).toBeDefined());
    expect(screen.getByTestId("sign-out-confirm").textContent).toContain("3");
    expect(purgeCaptureQueueMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("sign-out-confirm-cancel"));

    expect(screen.queryByTestId("sign-out-confirm")).toBeNull();
    // Still signed in — the dialog was dismissed, not confirmed.
    expect(screen.getByTestId("app-shell")).toBeDefined();
    expect(purgeCaptureQueueMock).not.toHaveBeenCalled();
  });

  it("MOB-009: confirming purges the queue with reason 'logout' before completing sign-out", async () => {
    capturesAtRiskMock.mockResolvedValueOnce(2);
    await signInAndOut();
    await waitFor(() => expect(screen.getByTestId("sign-out-confirm")).toBeDefined());

    fireEvent.click(screen.getByTestId("sign-out-confirm-anyway"));

    await waitFor(() => expect(screen.getByTestId("login-form")).toBeDefined());
    expect(purgeCaptureQueueMock).toHaveBeenCalledWith("logout");
  });
});

describe("The session bootstrap (GET /v1/me)", () => {
  it("shows the honest error state, with a way out, when the session cannot be loaded", async () => {
    // Not a blank screen and not a login form: the person IS signed in, and
    // the failure is ours. D5 — what happened, and the two things they can do.
    vi.stubGlobal(
      "fetch",
      routedFetch({ "GET /v1/me": () => new Response(null, { status: 503 }) }),
    );

    renderApp({ authenticated: true, language: "nl" }, { route: "/" });

    await waitFor(() => expect(screen.getByTestId("session-error")).toBeDefined());
    expect(screen.getByTestId("sign-out")).toBeDefined();
  });

  it("sends a business with no administration to onboarding", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        "GET /v1/me": () =>
          jsonResponse(
            meFixture({
              administrations: [],
              active_administration_id: null,
              onboarding: { needs_administration: true },
            }),
          ),
      }),
    );

    renderApp({ authenticated: true, language: "nl" }, { route: "/" });

    await waitFor(() => expect(screen.getByTestId("onboarding")).toBeDefined());
  });
});
