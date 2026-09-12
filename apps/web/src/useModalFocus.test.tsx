import { fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { describe, expect, it } from "vitest";

import { useModalFocus } from "./useModalFocus";

/**
 * WCAG 2.2 SC 2.4.3 — the hook both of this app's dialogs (ClientSwitcher,
 * CaptureScreen's QualityPrompt) now use. Tested directly, against a plain
 * two-button dialog, so the trap/restore mechanism is proven once rather
 * than only inferred from each caller's own, more complicated tests.
 */
function Dialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  useModalFocus(open, containerRef);
  if (!open) return null;
  return (
    <div ref={containerRef} role="dialog" aria-modal="true" data-testid="dialog">
      <button data-testid="first">first</button>
      <button data-testid="second">second</button>
      <button data-testid="close" onClick={onClose}>
        close
      </button>
    </div>
  );
}

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button data-testid="trigger" onClick={() => setOpen(true)}>
        open
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}

describe("useModalFocus", () => {
  it("focuses the first focusable element inside when it opens", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("trigger"));

    expect(document.activeElement).toBe(screen.getByTestId("first"));
  });

  it("wraps Tab from the last focusable element back to the first", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("trigger"));

    screen.getByTestId("close").focus();
    fireEvent.keyDown(document, { key: "Tab" });

    expect(document.activeElement).toBe(screen.getByTestId("first"));
  });

  it("wraps Shift+Tab from the first focusable element back to the last", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("trigger"));

    expect(document.activeElement).toBe(screen.getByTestId("first"));
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });

    expect(document.activeElement).toBe(screen.getByTestId("close"));
  });

  it("does not intervene on Tab between two elements that are both inside the trap", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("trigger"));

    screen.getByTestId("first").focus();
    // Tab from the FIRST of three is not a boundary case — nothing should
    // move focus on this component's behalf (a real browser's own Tab
    // handling does that from here; this only guards the two edges).
    fireEvent.keyDown(document, { key: "Tab" });

    expect(document.activeElement).toBe(screen.getByTestId("first"));
  });

  it("restores focus to the trigger when the dialog unmounts", () => {
    render(<Harness />);
    const trigger = screen.getByTestId("trigger");
    trigger.focus();

    fireEvent.click(trigger);
    expect(screen.getByTestId("dialog")).toBeTruthy();

    fireEvent.click(screen.getByTestId("close"));

    expect(screen.queryByTestId("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});
