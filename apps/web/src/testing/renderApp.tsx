import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { App } from "../App";

/**
 * The router context, wrapped around an element a suite renders itself.
 *
 * Use this rather than `renderApp` below when the suite needs the render
 * result's own `rerender` (which must be handed the same wrapper, or the tree
 * unmounts and the state under test is lost).
 */
export function inRouter(ui: ReactElement, route = "/"): ReactElement {
  return <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>;
}

type AppProps = NonNullable<Parameters<typeof App>[0]>;

/**
 * Mounts the whole app at a given URL.
 *
 * `App` renders `<Routes>` and nothing above it — `main.tsx` owns the
 * `BrowserRouter` (ADR-058) — precisely so a test can put it in a
 * `MemoryRouter` at any address instead of driving navigation through
 * clicks. Without one, every render throws "useRoutes() may be used only in
 * the context of a <Router>", which is the failure this helper exists to
 * stop each suite from solving its own way.
 *
 * `route` is the address to open at. Screens behind authentication also need
 * `GET /v1/me` answered (see `SessionProvider`); `authenticated` alone puts
 * the app past the door, not past the bootstrap.
 */
export function renderApp(props: AppProps = {}, { route = "/" }: { route?: string } = {}): RenderResult {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App {...props} />
    </MemoryRouter>,
  );
}
