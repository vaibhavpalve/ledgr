import { fireEvent, render as renderBare, screen, renderHook } from "@testing-library/react";
import type { ReactElement } from "react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { SwitcherEntry } from "@ledgr/shared-types";
import { ClientSwitcher, useSwitcherShortcut } from "./ClientSwitcher";

/**
 * Every string in these components comes from the catalogue (FR-LOC-001), so
 * they need a language to render in. `render` is shadowed rather than each
 * call site being changed, which keeps the tests below about the behaviour
 * they were written for.
 *
 * The default is Dutch, matching the product's (see DEFAULT_LANGUAGE and PRD
 * Q12). Tests that are ABOUT language pass one explicitly, so no assertion
 * silently depends on which default is in force.
 */
function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const entry = (over: Partial<SwitcherEntry> & { administrationId: string }): SwitcherEntry => ({
  displayName: "Bakker IT",
  legalName: "Bakker Consultancy B.V.",
  tradeName: "Bakker IT",
  kvkNumber: "12345678",
  colour: "indigo",
  initials: "BI",
  colourIsAmbiguous: false,
  role: "Accountant",
  roleIsSystem: true,
  expiresAt: null,
  ...over,
});

const BAKKER = entry({ administrationId: "a", displayName: "Bakker IT", initials: "BI" });
const DE_VRIES = entry({
  administrationId: "b",
  displayName: "De Vries Holding",
  initials: "DV",
  colour: "amber",
  kvkNumber: "87654321",
});
const JANSEN = entry({
  administrationId: "c",
  displayName: "Jansen Tandarts",
  initials: "JT",
  colour: "teal",
  kvkNumber: "12349999",
});
const THREE = [BAKKER, DE_VRIES, JANSEN];

function open(props: Partial<Parameters<typeof ClientSwitcher>[0]> = {}) {
  const onSelect = vi.fn();
  const onSearch = vi.fn();
  const onClose = vi.fn();
  render(
    <ClientSwitcher
      open
      entries={THREE}
      onSelect={onSelect}
      onSearch={onSearch}
      onClose={onClose}
      {...props}
    />,
  );
  return { onSelect, onSearch, onClose, input: screen.getByTestId("client-switcher-input") };
}

const activeOption = () =>
  screen.getAllByTestId("client-switcher-option").find((o) => o.dataset.active === "true");

describe("FR-FRM-000: the switcher is reachable by keyboard alone", () => {
  it("focuses the search field as soon as it opens", () => {
    const { input } = open();

    // Ctrl+K must land the caret where typing goes, not require a further Tab.
    expect(document.activeElement).toBe(input);
  });

  it("opens on Ctrl+K from anywhere on the page", () => {
    const onOpen = vi.fn();
    renderHook(() => useSwitcherShortcut(onOpen));

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("opens on Cmd+K too, for the same reason", () => {
    const onOpen = vi.fn();
    renderHook(() => useSwitcherShortcut(onOpen));

    fireEvent.keyDown(window, { key: "k", metaKey: true });

    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("does not steal a bare k", () => {
    const onOpen = vi.fn();
    renderHook(() => useSwitcherShortcut(onOpen));

    fireEvent.keyDown(window, { key: "k" });

    expect(onOpen).not.toHaveBeenCalled();
  });

  it("starts with the first result active, so type-then-Enter works", () => {
    const { input, onSelect } = open();

    fireEvent.keyDown(input, { key: "Enter" });

    // The API orders exact, then prefix, then substring, so position one is
    // what someone typing a specific name is reaching for.
    expect(onSelect).toHaveBeenCalledWith(BAKKER);
  });

  it("moves the active option with the arrow keys", () => {
    const { input } = open();

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(activeOption()?.dataset.administrationId).toBe("b");

    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(activeOption()?.dataset.administrationId).toBe("a");
  });

  it("wraps at both ends", () => {
    // Without wrapping, holding a key silently stops and a person believes
    // the list ended where it did not.
    const { input } = open();

    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(activeOption()?.dataset.administrationId).toBe("c");

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(activeOption()?.dataset.administrationId).toBe("a");
  });

  it("jumps to the ends with Home and End", () => {
    const { input } = open();

    fireEvent.keyDown(input, { key: "End" });
    expect(activeOption()?.dataset.administrationId).toBe("c");

    fireEvent.keyDown(input, { key: "Home" });
    expect(activeOption()?.dataset.administrationId).toBe("a");
  });

  it("selects the active option with Enter", () => {
    const { input, onSelect } = open();

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onSelect).toHaveBeenCalledWith(DE_VRIES);
  });

  it("closes on Escape without selecting anything", () => {
    const { input, onClose, onSelect } = open();

    fireEvent.keyDown(input, { key: "Escape" });

    expect(onClose).toHaveBeenCalledOnce();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("asks the API to search rather than filtering what it was given", () => {
    // Filtering client-side would mean the browser already received clients
    // the user may not see - the leak would have happened before it was hidden.
    const { input, onSearch } = open();

    fireEvent.change(input, { target: { value: "vries" } });

    expect(onSearch).toHaveBeenCalledWith("vries");
  });

  it("resets the active option when the query changes", () => {
    const { input } = open();
    fireEvent.keyDown(input, { key: "End" });

    fireEvent.change(input, { target: { value: "b" } });

    expect(activeOption()?.dataset.administrationId).toBe("a");
  });

  it("keeps Enter working when the result set shrinks under the cursor", () => {
    // A stale index past the end would make Enter select nothing and look
    // like a broken key.
    function Harness() {
      const [entries, setEntries] = useState(THREE);
      return (
        <>
          <button onClick={() => setEntries([BAKKER])}>shrink</button>
          <ClientSwitcher
            open
            entries={entries}
            onSearch={() => {}}
            onSelect={() => {}}
            onClose={() => {}}
          />
        </>
      );
    }
    render(<Harness />);
    const input = screen.getByTestId("client-switcher-input");
    fireEvent.keyDown(input, { key: "End" });

    fireEvent.click(screen.getByText("shrink"));

    expect(activeOption()?.dataset.administrationId).toBe("a");
  });

  it("says so when nothing matches, rather than showing an empty box", () => {
    render(
      <ClientSwitcher
        open
        entries={[]}
        onSearch={() => {}}
        onSelect={() => {}}
        onClose={() => {}}
      />,
    );

    expect(screen.getByTestId("client-switcher-empty")).toBeTruthy();
  });

  it("does not crash pressing Enter on an empty list", () => {
    const onSelect = vi.fn();
    render(
      <ClientSwitcher
        open
        entries={[]}
        onSearch={() => {}}
        onSelect={onSelect}
        onClose={() => {}}
      />,
    );

    fireEvent.keyDown(screen.getByTestId("client-switcher-input"), { key: "Enter" });

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("exposes itself to assistive technology as a combobox over a listbox", () => {
    const { input } = open();

    expect(input.getAttribute("role")).toBe("combobox");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    // aria-activedescendant rather than moving DOM focus, so typing keeps
    // working while the active option changes.
    expect(input.getAttribute("aria-activedescendant")).toBe(
      screen.getByTestId("client-switcher-list").querySelector("li")?.id,
    );
    expect(screen.getByTestId("client-switcher-list").getAttribute("role")).toBe("listbox");
  });

  it("marks which client is already open", () => {
    open({ activeAdministrationId: "b" });

    expect(screen.getByTestId("client-switcher-current")).toBeTruthy();
  });

  it("renders nothing at all when closed", () => {
    render(
      <ClientSwitcher
        open={false}
        entries={THREE}
        onSearch={() => {}}
        onSelect={() => {}}
        onClose={() => {}}
      />,
    );

    expect(screen.queryByTestId("client-switcher-input")).toBeNull();
  });
});

// FR-FRM-000a's ClientHeader coverage lives in its own ClientHeader.test.tsx
// (moved there so it sits beside the component it tests, alongside the new
// loading-state tri-state tests added for the mobile client header task).
