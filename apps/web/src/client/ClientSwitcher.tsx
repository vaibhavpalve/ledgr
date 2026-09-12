import { useEffect, useId, useMemo, useRef, useState } from "react";
import { roleLabel, useI18n } from "@ledgr/i18n";
import type { SwitcherEntry } from "@ledgr/shared-types";

import { useModalFocus } from "../useModalFocus";

/**
 * FR-FRM-000: "Client switcher: searchable by client name, KvK number or
 * trade name, keyboard-reachable, showing only granted administrations."
 *
 * --- What "keyboard-reachable" is taken to mean ---
 *
 * Not "focusable with Tab". A bookkeeper moving between clients dozens of
 * times a day needs the whole interaction to be reachable without leaving
 * the keyboard, so:
 *
 *   Ctrl/Cmd+K   open from anywhere, without tabbing to a trigger
 *   type         filter (the API orders exact → prefix → substring, so the
 *                first result is what a specific name was reaching for)
 *   ↓ / ↑        move the active option, wrapping at both ends
 *   Home / End   first / last
 *   Enter        switch to the active option
 *   Escape       close, returning focus to whatever had it before
 *
 * Wrapping at both ends matters more than it looks: without it, holding ↓
 * silently stops at the last entry, and a person who expected to cycle
 * believes the list ended somewhere it did not.
 *
 * --- Why the list is not filtered here ---
 *
 * Searching calls the API. "Showing only granted administrations" is a
 * tenancy guarantee, and filtering client-side would mean the browser had
 * received clients the user may not see — the leak would already have
 * happened by the time it was hidden. `onSearch` is debounce-free on
 * purpose: the caller owns that, and a component that debounced internally
 * would be untestable without fake timers.
 *
 * --- Accessibility ---
 *
 * ARIA combobox + listbox, with `aria-activedescendant` rather than moving
 * DOM focus, so typing continues to work while the active option changes.
 * Every option carries its name and role as text; the colour marker is
 * aria-hidden because it duplicates the name beside it.
 *
 * --- Language (FR-LOC-001) ---
 *
 * Every string here comes from the catalogue, the accessible names included —
 * `aria-label` is read aloud, so an untranslated one is an untranslated screen
 * for the person who depends on it most, and FR-LOC-004's WCAG commitment and
 * FR-LOC-001 are the same obligation there.
 *
 * The client's own NAME is not translated and must not be. Nor is `KvK`: it is
 * a statutory term that keeps its Dutch form in the English UI (FR-LOC-001c),
 * which is why `client.switcher.kvk` is one of the few records in the
 * catalogue marked `identical`.
 */
export function ClientSwitcher({
  entries,
  activeAdministrationId = null,
  onSearch,
  onSelect,
  onClose,
  open,
}: {
  entries: SwitcherEntry[];
  activeAdministrationId?: string | null;
  onSearch: (query: string) => void;
  onSelect: (entry: SwitcherEntry) => void;
  onClose: () => void;
  open: boolean;
}) {
  const { t, language } = useI18n();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  // WCAG 2.2 SC 2.4.3: focuses the input when opened (so Ctrl+K lands the
  // caret where typing goes, the input being the first — and only —
  // focusable element inside), traps Tab inside the dialog while it is open,
  // and returns focus to whatever had it (the trigger that opened the
  // switcher) once it closes — none of which `role="dialog"` does by itself.
  //
  // This SUBSUMES a plain `if (open) inputRef.current?.focus()` effect that
  // used to live here on its own: running both was a race, not a belt and
  // braces — whichever ran second would capture the OTHER one's just-applied
  // focus as "what had focus before the dialog opened", so closing would
  // restore focus to the dialog's own input instead of the real trigger. See
  // useModalFocus's own docstring.
  useModalFocus(open, dialogRef);

  // A shrinking result set must not leave the active index past the end —
  // Enter would then select nothing and look like a broken key.
  useEffect(() => {
    setActiveIndex((current) => (current >= entries.length ? 0 : current));
  }, [entries.length]);

  const activeEntry = useMemo(
    () => (activeIndex < entries.length ? entries[activeIndex] : undefined),
    [entries, activeIndex],
  );

  if (!open) {
    return null;
  }

  const move = (delta: number) => {
    if (entries.length === 0) return;
    setActiveIndex((current) => (current + delta + entries.length) % entries.length);
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        move(-1);
        break;
      case "Home":
        event.preventDefault();
        setActiveIndex(0);
        break;
      case "End":
        event.preventDefault();
        setActiveIndex(Math.max(entries.length - 1, 0));
        break;
      case "Enter":
        event.preventDefault();
        if (activeEntry) onSelect(activeEntry);
        break;
      case "Escape":
        event.preventDefault();
        onClose();
        break;
      default:
        break;
    }
  };

  return (
    <div
      ref={dialogRef}
      className="client-switcher"
      role="dialog"
      aria-modal="true"
      aria-label={t("client.switcher.dialog_label")}
    >
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded="true"
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-activedescendant={
          activeEntry ? `${listboxId}-${activeEntry.administrationId}` : undefined
        }
        aria-label={t("client.switcher.search_label")}
        placeholder={t("client.switcher.search_placeholder")}
        value={query}
        data-testid="client-switcher-input"
        onChange={(event) => {
          setQuery(event.target.value);
          setActiveIndex(0);
          onSearch(event.target.value);
        }}
        onKeyDown={handleKeyDown}
      />

      <ul
        id={listboxId}
        role="listbox"
        aria-label={t("client.switcher.list_label")}
        data-testid="client-switcher-list"
      >
        {entries.length === 0 ? (
          // A role="listbox" ARIA-requires its children to be options (or
          // option groups) — found by the axe-core tests in this file's
          // CMP-012/FR-LOC-004 describe block, which this repo's own manual
          // audit missed. `aria-disabled` rather than omitting the role
          // entirely: this row is still the listbox's one and only child and
          // has to identify as one of its permitted children, but it names
          // nothing selectable — the same shape a disabled `<option>` takes.
          <li
            role="option"
            aria-disabled="true"
            aria-selected="false"
            className="client-switcher__empty"
            data-testid="client-switcher-empty"
          >
            {t("client.switcher.no_matches", { query })}
          </li>
        ) : (
          entries.map((entry, index) => (
            // This option's keyboard path is Enter on the INPUT above (see
            // handleKeyDown), which selects `activeEntry` via
            // `aria-activedescendant` — the ARIA combobox pattern's whole
            // point is that the option itself never receives DOM focus or a
            // keydown event. `onClick` here is the mouse-only convenience
            // this component's own docstring describes; it is not a second,
            // unreachable-by-keyboard path, and adding a keydown handler
            // directly on a `role="option"` element that is never focused
            // would do nothing.
            // eslint-disable-next-line jsx-a11y/click-events-have-key-events
            <li
              key={entry.administrationId}
              id={`${listboxId}-${entry.administrationId}`}
              role="option"
              aria-selected={index === activeIndex}
              data-testid="client-switcher-option"
              data-active={index === activeIndex ? "true" : "false"}
              data-administration-id={entry.administrationId}
              className={index === activeIndex ? "is-active" : undefined}
              // Mouse users get the same behaviour; the keyboard path above
              // is the one the requirement names.
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => onSelect(entry)}
            >
              <span className={`client-marker client-marker--${entry.colour}`} aria-hidden="true">
                {entry.initials}
              </span>
              <span className="client-switcher__name">{entry.displayName}</span>
              {entry.kvkNumber ? (
                <span className="client-switcher__kvk">
                  {t("client.switcher.kvk", { number: entry.kvkNumber })}
                </span>
              ) : null}
              {/*
                Translated for one of §8.4's twelve system roles, verbatim for
                a custom one — `roleIsSystem` is the API's answer, because an
                organization may name a custom role "Bookkeeper" and rendering
                that as "Boekhouder" would show a role it does not have. The
                decision lives in @ledgr/i18n so mobile makes it the same way.
              */}
              <span className="client-switcher__role">
                {roleLabel(entry.role, language, { isSystem: entry.roleIsSystem })}
              </span>
              {entry.administrationId === activeAdministrationId ? (
                <span className="client-switcher__current" data-testid="client-switcher-current">
                  {t("client.switcher.current")}
                </span>
              ) : null}
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

/**
 * Ctrl/Cmd+K from anywhere. A hook rather than a listener inside the
 * component, because the switcher is not mounted while closed — a component
 * that has to be rendered to listen for the shortcut that opens it cannot
 * work.
 */
export function useSwitcherShortcut(onOpen: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onOpen();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onOpen]);
}
