import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";

import { LanguageSwitcher } from "../LanguageSwitcher";
import { ThemeToggle } from "../theme/ThemeToggle";
import { useModalFocus } from "../useModalFocus";
import { Icon } from "./icons";

/**
 * The avatar in the top-right of the canvas, opened: who is signed in, the
 * two presentation controls (language and appearance both live here now,
 * FR-LOC-001a's "from the user menu in one click"), the settings link, and
 * sign out.
 *
 * A dialog rather than a `role="menu"`: the panel holds two segmented
 * controls and a link, which are not menu items, and the menu pattern's
 * arrow-key semantics would fight `LanguageSwitcher`'s own buttons.
 * `useModalFocus` gives it the focus discipline every dialog in this app
 * has (ADR-053); Escape and a click outside close it.
 *
 * `extraLinks` is the compact layout's escape hatch: below 64rem the rail is
 * gone and the bottom bar holds five sections, so the rest of the product
 * (customers, grootboek, the portfolio) is reachable from here.
 */
export function UserMenu({
  email,
  onSignOut,
  extraLinks = [],
}: {
  email: string;
  onSignOut: () => void;
  extraLinks?: ReadonlyArray<{ to: string; label: string }>;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  useModalFocus(open, panelRef);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointer = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  return (
    <div className="user-menu" ref={rootRef}>
      <button
        type="button"
        className="user-menu__trigger"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={t("common.user_menu.open")}
        data-testid="user-menu-trigger"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="user-menu__avatar ledgr-num" aria-hidden="true">
          {initialsOf(email)}
        </span>
      </button>

      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-modal="true"
          aria-label={t("common.user_menu.label")}
          className="user-menu__panel"
          data-testid="user-menu"
        >
          <p className="user-menu__email" data-testid="user-menu-email">
            {email}
          </p>

          <div className="user-menu__section">
            <p className="label">{t("common.language.label")}</p>
            <LanguageSwitcher />
          </div>
          <div className="user-menu__section">
            <p className="label">{t("common.theme.label")}</p>
            <ThemeToggle />
          </div>

          <nav className="user-menu__links" aria-label={t("common.user_menu.label")}>
            {extraLinks.map((link) => (
              <Link
                key={link.to}
                to={link.to}
                className="user-menu__link user-menu__link--compact-only"
                onClick={() => setOpen(false)}
              >
                {link.label}
              </Link>
            ))}
            <Link
              to="/settings/profile"
              className="user-menu__link"
              data-testid="user-menu-settings"
              onClick={() => setOpen(false)}
            >
              <Icon name="settings" size={18} />
              {t("common.nav.settings")}
            </Link>
          </nav>

          <button
            type="button"
            className="button--quiet user-menu__sign-out"
            data-testid="sign-out"
            onClick={() => {
              setOpen(false);
              onSignOut();
            }}
          >
            {t("auth.sign_out")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** Two letters from the local part of an address: `sanne.bakker@` → `SB`. */
export function initialsOf(email: string): string {
  const local = email.split("@")[0] ?? "";
  const parts = local.split(/[._\-+]/).filter((part) => part.length > 0);
  const letters =
    parts.length >= 2
      ? `${parts[0]?.[0] ?? ""}${parts[1]?.[0] ?? ""}`
      : local.slice(0, 2);
  return letters.toUpperCase() || "?";
}
