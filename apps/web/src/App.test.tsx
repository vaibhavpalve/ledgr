import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { App } from "./App";
import { MemoryKeyVault, WebCryptoCipher } from "./capture/webCryptoCipher";
import type { SittingContext } from "./capture/useSitting";

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
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

// `AuthenticatedMobileShell` calls `captureQueue()`, whose real composition
// (capture/queue.ts) opens an IndexedDB database — unavailable in jsdom, the
// same reason every other suite touching the capture screen (e.g.
// CaptureScreen.test.tsx, MobileShell.test.tsx) builds its own `CaptureQueue`
// over a `MemoryStore` rather than going through the module singleton. Mocked
// here so App.test.tsx can exercise the real composition root's WIRING
// (does authenticated + a mobile context reach MobileShell) without needing a
// real IndexedDB.
vi.mock("./capture/queue", () => ({
  captureQueue: () =>
    new CaptureQueue({
      store: new MemoryStore(),
      cipher: new WebCryptoCipher(new MemoryKeyVault()),
      clock: { now: () => Date.now() },
      newId: () => crypto.randomUUID(),
    }),
}));

const mobileContext: SittingContext = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
};

describe("App", () => {
  it("renders without crashing", () => {
    render(<App />);
    expect(screen.getByText("LEDGR")).toBeDefined();
  });

  it("authenticated with no mobile context renders today's bare header only — the pre-existing, documented gap", () => {
    render(<App authenticated />);
    expect(screen.getByText("LEDGR")).toBeDefined();
    expect(screen.queryByTestId("mobile-shell-tabs")).toBeNull();
  });

  it("authenticated with a mobile context renders the five-tab mobile shell, opening on Home", async () => {
    render(<App authenticated mobileContext={mobileContext} />);
    expect(screen.getByTestId("mobile-shell-tabs")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-home")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-capture")).toBeDefined();
    // FR-UX-005: Home, not Capture, is where the shell opens.
    expect(screen.getByTestId("mobile-tab-home").getAttribute("aria-current")).toBe("page");

    // HomeScreen fetches the dashboard as soon as it mounts, which here means
    // a real `fetch` against a relative URL — the one thing this
    // composition-root test does not mock. Waited out so that its
    // (necessarily failing) settlement lands inside this test rather than as
    // an unwrapped state update blamed on whichever test runs next.
    await waitFor(() => expect(screen.getByTestId("home-error")).toBeDefined());
  });

  it("switching to Capture from Home still starts a sitting on its own (ADR-047)", async () => {
    render(<App authenticated mobileContext={mobileContext} />);
    await waitFor(() => expect(screen.getByTestId("home-error")).toBeDefined());

    fireEvent.click(screen.getByTestId("mobile-tab-capture"));

    // ADR-047: the capture screen starts a sitting on its own as soon as it
    // mounts, which here means a real `fetch` against a relative URL — the
    // one thing this composition-root test does not mock.
    await waitFor(() =>
      expect(screen.getByTestId("capture-problem").getAttribute("data-reason")).toBe(
        "offline_cannot_start",
      ),
    );
  });
});

/** Routes a stubbed `fetch` by URL, matching the badge endpoint and failing everything else. */
function fetchRoutedTo(activeClientResponse: () => Promise<Response> | Response) {
  return vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === "/v1/switcher/active") return activeClientResponse();
    // Every other call (dashboard, capture-sessions, ...) fails the same way
    // it does in the tests above, which don't stub fetch at all - this
    // describe block is only about the badge fetch.
    return new Response(null, { status: 500 });
  });
}

const activeBadgeWire = {
  administration_id: "adm-A",
  display_name: "Bakker IT",
  legal_name: "Bakker Consultancy B.V.",
  trade_name: "Bakker IT",
  kvk_number: "12345678",
  colour: "indigo",
  initials: "BI",
  colour_is_ambiguous: false,
  role: "Accountant",
  role_is_system: true,
  expires_at: null,
};

describe("AuthenticatedMobileShell: fetches the active-client badge on mount (FR-FRM-000a)", () => {
  it("fetches GET /v1/switcher/active and passes the resolved badge through to the header", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRoutedTo(
        () =>
          new Response(JSON.stringify(activeBadgeWire), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );

    render(<App authenticated mobileContext={mobileContext} language="nl" />);

    await waitFor(() => expect(screen.getByTestId("client-name").textContent).toBe("Bakker IT"));
    expect(screen.getByTestId("client-header").dataset.state).toBe("active");
  });

  it(
    "shows the neutral loading state while the fetch is in flight, and only renders " +
      "'no client selected' once the API has confirmed there is none - never the reverse order",
    async () => {
      let resolveFetch: (response: Response) => void = () => {};
      const pending = new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
      vi.stubGlobal(
        "fetch",
        fetchRoutedTo(() => pending),
      );

      render(<App authenticated mobileContext={mobileContext} language="nl" />);

      // The fetch has not answered yet: the header must show the neutral
      // loading state, not "no client selected" - that would be a false
      // signal for the entire window the request is in flight.
      expect(screen.getByTestId("client-header").dataset.state).toBe("loading");
      expect(screen.queryByText("Geen klant geselecteerd")).toBeNull();

      resolveFetch(
        new Response("null", { status: 200, headers: { "Content-Type": "application/json" } }),
      );

      await waitFor(() => expect(screen.getByTestId("client-header").dataset.state).toBe("none"));
      expect(screen.getByText("Geen klant geselecteerd")).toBeDefined();
    },
  );
});

/** Routes a stubbed `fetch` by method + path, for the ADR-054 wiring tests
 * below - `Shell` calls several real endpoints across one flow (login, MFA
 * enrolment, the account-language read on first login), and each needs its
 * own answer rather than one blanket stub. */
function routedFetch(handlers: Record<string, () => Response>): typeof fetch {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    const handler = handlers[key];
    if (handler) return handler();
    // GET /v1/me/language fires once `authenticated` becomes true
    // (useAccountLanguageOnFirstLogin) - answered here with "never chosen"
    // so it never needs its own entry in every test below.
    if (key === "GET /v1/me/language") {
      return new Response(JSON.stringify({ language: null, supported: ["en", "nl"] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(null, { status: 500 });
  }) as unknown as typeof fetch;
}

describe("ADR-054: signup/login/MFA wired end to end through Shell", () => {
  it("a password login that has not cleared MFA lands on the enrolment gate, never the app", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        "POST /v1/auth/login": () =>
          new Response(
            JSON.stringify({
              access_token: "tok",
              token_type: "bearer",
              mfa_verified: false,
              mfa: { has_passkey: false, has_totp: false },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      }),
    );

    render(<App language="nl" />);
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() => expect(screen.getByTestId("mfa-enrollment")).toBeDefined());
    expect(screen.queryByTestId("sign-out")).toBeNull();
  });

  it("completing TOTP enrolment on the gate moves into the authenticated shell", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        "POST /v1/auth/login": () =>
          new Response(
            JSON.stringify({
              access_token: "tok",
              token_type: "bearer",
              mfa_verified: false,
              mfa: { has_passkey: false, has_totp: false },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        "POST /v1/auth/mfa/totp/enroll/begin": () =>
          new Response(
            JSON.stringify({ secret: "JBSWY3DPEHPK3PXP", provisioning_uri: "otpauth://x" }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        "POST /v1/auth/mfa/totp/enroll/confirm": () =>
          new Response(
            JSON.stringify({ access_token: "tok2", token_type: "bearer", mfa_verified: true }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      }),
    );

    render(<App language="nl" />);
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() => expect(screen.getByTestId("mfa-totp-begin")).toBeDefined());
    fireEvent.click(screen.getByTestId("mfa-totp-begin"));
    await waitFor(() => expect(screen.getByTestId("mfa-totp-code")).toBeDefined());
    fireEvent.change(screen.getByTestId("mfa-totp-code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByTestId("mfa-totp-confirm"));

    await waitFor(() => expect(screen.getByTestId("sign-out")).toBeDefined());
    expect(screen.queryByTestId("mfa-enrollment")).toBeNull();
  });

  it("signing out returns to the login screen, and a second login starts unauthenticated again", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetch({
        "POST /v1/auth/login": () =>
          new Response(
            JSON.stringify({ access_token: "tok", token_type: "bearer", mfa_verified: true }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        "POST /v1/auth/logout": () =>
          new Response(JSON.stringify({ status: "logged_out" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      }),
    );

    render(<App language="nl" />);
    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() => expect(screen.getByTestId("sign-out")).toBeDefined());

    fireEvent.click(screen.getByTestId("sign-out"));

    await waitFor(() => expect(screen.getByTestId("login-form")).toBeDefined());
  });

  it("a Google OAuth redirect landing (?code&state in the URL) shows the callback screen, not the login form", async () => {
    window.history.pushState({}, "", "/?code=auth-code&state=state-value");
    const fetchStub = routedFetch({
      "POST /v1/auth/login/google/callback": () =>
        new Response(
          JSON.stringify({ access_token: "tok", token_type: "bearer", mfa_verified: true }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    vi.stubGlobal("fetch", fetchStub);

    render(<App language="nl" />);

    expect(screen.queryByTestId("login-form")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("sign-out")).toBeDefined());
    expect(fetchStub).toHaveBeenCalled();
  });

  it("switching to signup and back to login preserves the frame, wiring both forms to the same Shell", () => {
    vi.stubGlobal("fetch", routedFetch({}));
    render(<App language="nl" />);

    fireEvent.click(screen.getByTestId("switch-to-signup"));
    expect(screen.getByTestId("signup-form")).toBeDefined();

    fireEvent.click(screen.getByTestId("switch-to-login"));
    expect(screen.getByTestId("login-form")).toBeDefined();
  });
});
