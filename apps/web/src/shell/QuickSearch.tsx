import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { SwitcherEntry } from "@ledgr/shared-types";

import { useSession } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { useModalFocus } from "../useModalFocus";
import { Icon } from "./icons";

/**
 * The "Zoek in alles… Ctrl K" field of the canvas, made honest: a palette
 * that reaches every screen by name and, through `GET /v1/switcher/search`,
 * every client the caller may open (FR-FRM-000's search shape, server-side).
 *
 * A search box that did nothing would be the dead end D5 forbids; a search
 * over financial records is FR-RPT/NFR-008's own feature and is not
 * this. What this does is exactly what a keyboard-first bookkeeper
 * (FR-WEB-002) reaches for Ctrl+K to do: go somewhere, or open a client.
 *
 * Same ARIA shape as `ClientSwitcher` (combobox + listbox,
 * `aria-activedescendant`), same keyboard set: ↓/↑ wrap, Enter chooses,
 * Escape closes and focus returns to the trigger (`useModalFocus`).
 */
interface Option {
  readonly id: string;
  readonly kind: "screen" | "client";
  readonly label: string;
  readonly detail: string | null;
  readonly entry?: SwitcherEntry;
  readonly to?: string;
}

export const SEARCHABLE_SCREENS: ReadonlyArray<{ to: string; key: string }> = [
  { to: "/", key: "common.nav.home" },
  { to: "/capture", key: "common.nav.capture" },
  { to: "/review", key: "common.nav.review" },
  { to: "/overview", key: "common.nav.overview" },
  { to: "/invoices", key: "common.nav.invoices" },
  { to: "/invoices/new", key: "invoice.new.title" },
  { to: "/customers", key: "common.nav.customers" },
  { to: "/customers/new", key: "customers.new.title" },
  { to: "/ledger", key: "common.nav.ledger" },
  { to: "/bank", key: "common.nav.bank" },
  { to: "/journal", key: "common.nav.journal" },
  { to: "/assets", key: "common.nav.assets" },
  { to: "/reports", key: "common.nav.reports" },
  { to: "/settings/profile", key: "common.nav.settings" },
  { to: "/settings/security", key: "settings.security.title" },
  { to: "/settings/invoice-design", key: "settings.invoice_design.title" },
];

export function QuickSearch({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { client } = useServices();
  const { administration, switchAdministration, me } = useSession();
  const [query, setQuery] = useState("");
  const [clients, setClients] = useState<readonly SwitcherEntry[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  useModalFocus(open, dialogRef);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    // Only a firm has clients to switch between; a business's own
    // administration is already open, and listing it would offer a switch
    // to where the person already is.
    if (me.organization.kind !== "firm") return;
    let cancelled = false;
    client
      .searchSwitcher("")
      .then((entries) => {
        if (!cancelled) setClients(entries);
      })
      .catch(() => {
        // A failed client search costs the client rows, not the screens.
      });
    return () => {
      cancelled = true;
    };
  }, [open, client, me.organization.kind]);

  const options = useMemo<Option[]>(() => {
    const needle = query.trim().toLowerCase();
    const screens: Option[] = SEARCHABLE_SCREENS.map((screen): Option => ({
      id: `screen:${screen.to}`,
      kind: "screen",
      label: t(screen.key),
      detail: null,
      to: screen.to,
    })).filter((option) => needle === "" || option.label.toLowerCase().includes(needle));
    const clientOptions: Option[] = clients
      .filter(
        (entry) =>
          needle === "" ||
          entry.displayName.toLowerCase().includes(needle) ||
          entry.legalName.toLowerCase().includes(needle) ||
          (entry.kvkNumber ?? "").includes(needle),
      )
      .map((entry) => ({
        id: `client:${entry.administrationId}`,
        kind: "client",
        label: entry.displayName,
        detail:
          entry.kvkNumber === null ? null : t("client.switcher.kvk", { number: entry.kvkNumber }),
        entry,
      }));
    return [...clientOptions, ...screens];
  }, [query, clients, t]);

  useEffect(() => {
    setActiveIndex((current) => (current >= options.length ? 0 : current));
  }, [options.length]);

  if (!open) return null;

  const active = options[activeIndex];

  const choose = (option: Option | undefined) => {
    if (option === undefined) return;
    onClose();
    if (option.kind === "screen" && option.to !== undefined) {
      navigate(option.to);
    } else if (option.entry !== undefined) {
      if (option.entry.administrationId !== administration?.id) {
        void switchAdministration(option.entry.administrationId).then(() => navigate("/"));
      }
    }
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActiveIndex((current) => (options.length === 0 ? 0 : (current + 1) % options.length));
        break;
      case "ArrowUp":
        event.preventDefault();
        setActiveIndex((current) =>
          options.length === 0 ? 0 : (current - 1 + options.length) % options.length,
        );
        break;
      case "Enter":
        event.preventDefault();
        choose(active);
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
    <div className="dialog-backdrop dialog-backdrop--top">
      <div
        ref={dialogRef}
        className="dialog quick-search"
        role="dialog"
        aria-modal="true"
        aria-label={t("common.search.label")}
        data-testid="quick-search"
      >
        <div className="quick-search__field">
          <Icon name="search" size={18} />
          <input
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-autocomplete="list"
            aria-activedescendant={active ? `${listboxId}-${active.id}` : undefined}
            aria-label={t("common.search.label")}
            placeholder={t("common.search.placeholder")}
            value={query}
            data-testid="quick-search-input"
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={onKeyDown}
          />
        </div>
        <ul
          id={listboxId}
          role="listbox"
          aria-label={t("common.search.results")}
          className="quick-search__list"
        >
          {options.length === 0 ? (
            <li
              role="option"
              aria-disabled="true"
              aria-selected="false"
              className="quick-search__empty"
            >
              {t("common.search.no_matches", { query })}
            </li>
          ) : (
            options.map((option, index) => (
              // Keyboard path is Enter on the combobox via aria-activedescendant
              // (the same reasoning ClientSwitcher records); the click is the
              // mouse convenience.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events
              <li
                key={option.id}
                id={`${listboxId}-${option.id}`}
                role="option"
                aria-selected={index === activeIndex}
                className={
                  index === activeIndex ? "quick-search__option is-active" : "quick-search__option"
                }
                data-testid={`quick-search-option-${option.kind}`}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(option)}
              >
                {option.entry !== undefined ? (
                  <span
                    className={`client-marker client-marker--${option.entry.colour}`}
                    aria-hidden="true"
                  >
                    {option.entry.initials}
                  </span>
                ) : (
                  <span className="quick-search__screen-icon" aria-hidden="true">
                    <Icon name="arrow-right" size={16} />
                  </span>
                )}
                <span className="quick-search__label">{option.label}</span>
                {option.detail !== null ? (
                  <span className="quick-search__detail ledgr-num">{option.detail}</span>
                ) : null}
                <span className="chip">
                  {option.kind === "client"
                    ? t("common.search.kind_client")
                    : t("common.search.kind_screen")}
                </span>
              </li>
            ))
          )}
        </ul>
        <p className="quick-search__hint caption">{t("common.search.hint")}</p>
      </div>
    </div>
  );
}
