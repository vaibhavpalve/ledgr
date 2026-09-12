import { useI18n } from "@ledgr/i18n";
import type { ClientBadge } from "@ledgr/shared-types";

/**
 * FR-FRM-000a: "The active client is unmistakable at all times — persistent
 * name and colour marker in the header on every screen."
 *
 * Three signals, deliberately redundant, because the requirement names a
 * harm ("posting to the wrong client is the single worst usability failure
 * in this product") rather than a decoration:
 *
 *   1. The NAME, always, in text. This is the authoritative signal and the
 *      only one that survives every rendering.
 *   2. The INITIALS, in the marker itself, so a client is still identified
 *      when colour is lost — to a colour-vision deficiency, a monochrome
 *      print, or a narrow viewport that clips the name.
 *   3. The COLOUR, which is what makes a wrong client noticeable at a glance
 *      before anything is submitted.
 *
 * Colour is the weakest of the three and is never alone. When the API
 * reports `colourIsAmbiguous` — another client in this user's switcher
 * shares the marker — the component says so rather than letting a person
 * rely on a signal that is not distinguishing for them.
 *
 * `badge === null` means the session is not inside any client. That renders
 * as an explicit "no client selected" rather than an empty header: a blank
 * space is indistinguishable from a header that failed to load, and the one
 * thing this component must never do is let someone assume a client is open
 * when none is.
 *
 * --- Three states, not two: `undefined` is not `null` ---
 *
 * `badge` is `ClientBadge | null | undefined`:
 *
 *   ClientBadge   an active client — rendered by name, marker and initials.
 *   null          CONFIRMED by the API: the session is not inside any
 *                 client. Renders "no client selected".
 *   undefined     NOT YET KNOWN — the caller has not heard back from
 *                 `GET /v1/switcher/active` yet. Renders a neutral loading
 *                 state instead.
 *
 * The caller (`AuthenticatedMobileShell` in `App.tsx`) is what tracks this
 * third state — this component only mirrors it. Collapsing `undefined` into
 * `null` would render "No client selected" for a moment on every cold start,
 * which is exactly the kind of momentary wrong signal this requirement
 * exists to rule out (see ADR-049).
 *
 * --- What is translated here, and what is not ---
 *
 * The two messages are (FR-LOC-001). The client's NAME, trade name and
 * initials are not, and never will be: they are the client's own name, and a
 * header that rendered one differently for two colleagues would defeat the
 * requirement it exists for. Nothing on this component is formatted either —
 * there are no figures on it — but when there are, they follow the
 * administration's locale rather than the reader's language (FR-LOC-002).
 */
export function ClientHeader({ badge }: { badge: ClientBadge | null | undefined }) {
  const { t } = useI18n();

  if (badge === undefined) {
    return (
      <header
        className="client-header client-header--loading"
        data-testid="client-header"
        data-state="loading"
      >
        <span className="client-header__name" role="status">
          {t("client.header.loading")}
        </span>
      </header>
    );
  }

  if (badge === null) {
    return (
      <header
        className="client-header client-header--empty"
        data-testid="client-header"
        data-state="none"
      >
        <span className="client-header__name">{t("client.header.none_selected")}</span>
      </header>
    );
  }

  return (
    <header
      className="client-header"
      data-testid="client-header"
      data-state="active"
      data-colour={badge.colour}
      data-administration-id={badge.administrationId}
    >
      <span
        className={`client-marker client-marker--${badge.colour}`}
        data-testid="client-marker"
        // The marker is decorative *as an image*: everything it conveys is
        // also in the text beside it, so a screen reader announcing the
        // initials twice would be noise.
        aria-hidden="true"
      >
        {badge.initials}
      </span>
      <span className="client-header__name" data-testid="client-name">
        {badge.displayName}
      </span>
      {badge.tradeName !== null && badge.tradeName !== badge.legalName ? (
        <span className="client-header__legal-name" data-testid="client-legal-name">
          {badge.legalName}
        </span>
      ) : null}
      {badge.colourIsAmbiguous ? (
        <span className="client-header__ambiguous" data-testid="client-colour-ambiguous">
          {t("client.header.colour_ambiguous")}
        </span>
      ) : null}
    </header>
  );
}
