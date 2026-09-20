import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { App } from "../App";
import { jsonResponse } from "../testing/fakeFetch";
import { meFixture } from "../testing/session";

/**
 * The session bootstrap under StrictMode — which is how the app actually runs
 * in development (`main.tsx` wraps the tree in it), and the one thing every
 * other suite here does not do: Testing Library's `render` mounts without it.
 *
 * React deliberately mounts, runs every effect, runs its cleanup, and runs the
 * effects again, to surface effects that cannot survive being re-run. The
 * session load could not: its "already started" guard turned the second run
 * away while the first run's cleanup had already marked its result to be
 * discarded, so the phase was never set and the app sat on its loading
 * skeleton forever. Every `pnpm dev` session after sign-in hit it; the
 * production build did not, and neither did any test, until the app was opened
 * in a browser.
 */
describe("SessionProvider under StrictMode", () => {
  function renderStrict(fetchImpl: typeof fetch) {
    vi.stubGlobal("fetch", fetchImpl);
    return render(
      <StrictMode>
        <MemoryRouter initialEntries={["/"]}>
          <App authenticated language="nl" />
        </MemoryRouter>
      </StrictMode>,
    );
  }

  it("reaches the app rather than sitting on the loading skeleton", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/v1/me") return jsonResponse(meFixture());
      return new Response(null, { status: 503 });
    }) as unknown as typeof fetch;

    renderStrict(fetchImpl);

    await waitFor(() => expect(screen.getByTestId("app-shell")).toBeDefined());
    expect(screen.queryByTestId("session-loading")).toBeNull();
  });

  it("asks the server once, not once per effect invocation", async () => {
    // The other half of the same fix: the second invocation adopts the load
    // already in flight instead of starting a duplicate, so a development
    // mount does not double every request the session makes.
    const calls: string[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input));
      if (String(input) === "/v1/me") return jsonResponse(meFixture());
      return new Response(null, { status: 503 });
    }) as unknown as typeof fetch;

    renderStrict(fetchImpl);

    await waitFor(() => expect(screen.getByTestId("app-shell")).toBeDefined());
    expect(calls.filter((url) => url === "/v1/me")).toHaveLength(1);
  });

  it("still shows the error state, with a way out, when the load fails", async () => {
    const fetchImpl = vi.fn(
      async () => new Response(null, { status: 503 }),
    ) as unknown as typeof fetch;

    renderStrict(fetchImpl);

    await waitFor(() => expect(screen.getByTestId("session-error")).toBeDefined());
    expect(screen.getByTestId("sign-out")).toBeDefined();
  });
});
