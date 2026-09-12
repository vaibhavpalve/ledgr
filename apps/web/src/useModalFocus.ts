import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/**
 * WCAG 2.2 SC 2.4.3 (Focus Order), for a `role="dialog"`/`role="alertdialog"`
 * built without a native `<dialog>` element and without a router to lean on
 * (this app's audit found two: ClientSwitcher and CaptureScreen's
 * QualityPrompt). ARIA attributes describe the role; they do not trap focus
 * or restore it, and neither did without this.
 *
 * Two things every dialog in this app needs, done once here instead of
 * twice:
 *
 *   1. Tab stays INSIDE it while it is open. Without this, a sighted keyboard
 *      user can tab into buttons elsewhere on the same screen while a
 *      decision the dialog exists to force (retake or use anyway; switch
 *      client or don't) is still open — which defeats the point of it being
 *      a dialog rather than an inline notice.
 *   2. Focus returns to whatever opened it when it closes. Without this,
 *      focus falls back to `<body>` and a keyboard user loses their place in
 *      the page for every dialog they close.
 *
 * `containerRef` must point at the dialog's own root element (the node
 * carrying `role="dialog"`/`role="alertdialog"`). This hook does not render
 * anything, and every dialog it is used on today is conditionally rendered
 * (returns `null` while closed) rather than always-mounted-and-hidden — the
 * effect's cleanup, which runs on that unmount, is what restores focus.
 */
export function useModalFocus(open: boolean, containerRef: RefObject<HTMLElement>): void {
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;

    previouslyFocused.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const focusable = (): HTMLElement[] => {
      const container = containerRef.current;
      return container
        ? Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        : [];
    };

    // Move focus inside unless a caller already has — ClientSwitcher, for
    // instance, focuses its own search input for Ctrl/Cmd+K before this ever
    // runs, and re-focusing the same element is harmless but redundant.
    if (!containerRef.current?.contains(document.activeElement)) {
      focusable()[0]?.focus();
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) {
        // Nothing focusable inside at all — Tab must not be allowed to leave
        // a dialog that is still open.
        event.preventDefault();
        return;
      }
      const first = elements[0]!;
      const last = elements[elements.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    // Capture phase, so this sees the key before anything inside the dialog
    // (e.g. ClientSwitcher's own input onKeyDown) has a chance to stop
    // propagation on it for an unrelated reason.
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      if (previouslyFocused.current && document.contains(previouslyFocused.current)) {
        previouslyFocused.current.focus();
      }
    };
  }, [open, containerRef]);
}
