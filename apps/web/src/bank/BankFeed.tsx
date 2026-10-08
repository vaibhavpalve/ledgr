import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  BankAccountView,
  BankFeedConnectionView,
  BankFeedInstitutionView,
  BankFeedStatusView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { ErrorState, LoadingSkeleton } from "../shell/ScreenState";
import { useModalFocus } from "../useModalFocus";

/**
 * The live bank feed on the Bank screen (FR-BNK-001, ADR-108).
 *
 * Shown only where the deployment has a provider configured; without one, the screen stays what it
 * was, statement import. The flow:
 *
 *   Connect your bank  ->  choose the bank  ->  the bank's own consent page (a full redirect)
 *   ->  /bank/feed-return?ref=<connection>  ->  the consent is checked and the first lines read
 *   ->  back to /bank with a one-line notice
 *
 * Then: "Live from your bank", when it last fetched, how long access lasts, "Fetch now" and
 * "Disconnect". A connection that failed or expired says why, in the reader's language, from the
 * reason code the API recorded on it.
 */

export type FeedNotice =
  | { readonly kind: "connected"; readonly bank: string }
  | { readonly kind: "pending" }
  | { readonly kind: "failed"; readonly reason: string };

const KNOWN_REASONS = new Set([
  "bank_feed_consent_failed",
  "bank_feed_account_mismatch",
  "bank_feed_consent_expired",
  "bank_feed_provider_unavailable",
  "bank_feed_consent_abandoned",
]);

/** A reason code to the reader's sentence (a named type: an inline arrow type here reads as JSX text to scripts/check_translations.py). */
type ReasonText = (code: string | null) => string | null;

function useReason(): ReasonText {
  const { t } = useI18n();
  return (code) =>
    code === null
      ? null
      : t(`bank.feed.reason.${KNOWN_REASONS.has(code) ? code : "bank_feed_consent_failed"}`);
}

export function FeedNoticeLine({ notice }: { notice: FeedNotice }) {
  const { t } = useI18n();
  const reason = useReason();
  if (notice.kind === "connected") {
    return (
      <p className="alert alert--positive" role="status" data-testid="bank-feed-notice">
        {t("bank.feed.connected", { bank: notice.bank })}
      </p>
    );
  }
  if (notice.kind === "pending") {
    return (
      <p className="alert alert--caution" role="status" data-testid="bank-feed-notice">
        {t("bank.feed.still_pending")}
      </p>
    );
  }
  return (
    <p className="alert alert--attention" role="alert" data-testid="bank-feed-notice">
      {reason(notice.reason)}
    </p>
  );
}

export function BankFeedPanel({
  administrationId,
  account,
  onFetched,
}: {
  administrationId: string;
  account: BankAccountView;
  /** New lines arrived: the transactions list should read again. */
  onFetched: () => void;
}) {
  const { t, date } = useI18n();
  const { bank } = useServices();
  const reason = useReason();
  const [status, setStatus] = useState<BankFeedStatusView | null>(null);
  const [busy, setBusy] = useState<"sync" | "disconnect" | null>(null);
  const [message, setMessage] = useState<{ tone: "positive" | "attention"; text: string } | null>(
    null,
  );
  const [choosing, setChoosing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setStatus(null);
    setMessage(null);
    // Secondary to the screen: a feed that cannot be read just does not show.
    void Promise.resolve()
      .then(() => bank.getFeed(administrationId, account.id))
      .then((result) => {
        if (!cancelled) setStatus(result);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [bank, administrationId, account.id]);

  if (status === null || !status.configured) return null;
  const connection = status.connection;
  const replace = (next: BankFeedConnectionView) =>
    setStatus((current) => (current ? { ...current, connection: next } : current));

  const sync = async () => {
    setBusy("sync");
    setMessage(null);
    try {
      const result = await bank.syncFeed(administrationId, account.id);
      replace(result.connection);
      if (result.imported !== null) {
        setMessage({
          tone: "positive",
          text: t("bank.feed.sync_result", { count: result.imported.transaction_count }),
        });
        if (result.imported.transaction_count > 0) onFetched();
      }
    } catch (error) {
      setMessage({ tone: "attention", text: describeError(error) });
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async () => {
    setBusy("disconnect");
    setMessage(null);
    try {
      replace(await bank.disconnectFeed(administrationId, account.id));
      setMessage({ tone: "positive", text: t("bank.feed.disconnected") });
    } catch (error) {
      setMessage({ tone: "attention", text: describeError(error) });
    } finally {
      setBusy(null);
    }
  };

  const linked = connection?.status === "linked";
  const pending = connection?.status === "pending";
  const why = connection && connection.status !== "revoked" ? reason(connection.last_error) : null;

  return (
    <div className="bank-feed" data-testid="bank-feed">
      {linked && connection ? (
        <div className="bank-feed__state">
          <span className="chip chip--positive" data-testid="bank-feed-live">
            <Icon name="check" size={12} />
            {t("bank.feed.live")}
          </span>
          {connection.institution_name ? (
            <span className="bank-feed__meta">
              {t("bank.feed.via", { bank: connection.institution_name })}
            </span>
          ) : null}
          <span className="bank-feed__meta" data-testid="bank-feed-synced">
            {connection.last_synced_at
              ? t("bank.feed.last_synced", { date: date(connection.last_synced_at.slice(0, 10)) })
              : t("bank.feed.never_synced")}
            {connection.consent_expires_at
              ? ` · ${t("bank.feed.consent_until", {
                  date: date(connection.consent_expires_at.slice(0, 10)),
                })}`
              : null}
          </span>
        </div>
      ) : (
        <p className="bank-feed__hint">
          {pending ? t("bank.feed.pending") : t("bank.feed.connect_hint")}
        </p>
      )}
      <div className="bank-feed__actions">
        {linked ? (
          <>
            <button
              type="button"
              className="button-link"
              disabled={busy !== null}
              onClick={() => void sync()}
              data-testid="bank-feed-sync"
            >
              {busy === "sync" ? t("bank.feed.syncing") : t("bank.feed.sync")}
            </button>
            <button
              type="button"
              className="button--quiet"
              disabled={busy !== null}
              onClick={() => void disconnect()}
              data-testid="bank-feed-disconnect"
            >
              {t("bank.feed.disconnect")}
            </button>
          </>
        ) : (
          <button
            type="button"
            className="button-link"
            onClick={() => setChoosing(true)}
            data-testid="bank-feed-connect"
          >
            <Icon name="plus" size={16} />{" "}
            {pending
              ? t("bank.feed.start_again")
              : connection && connection.status !== "revoked"
                ? t("bank.feed.reconnect")
                : t("bank.feed.connect")}
          </button>
        )}
      </div>
      {why !== null ? (
        <p className="bank-feed__why" data-testid="bank-feed-reason">
          <Icon name="warning" size={14} /> {why}
        </p>
      ) : null}
      {message !== null ? (
        <p
          className={`alert alert--${message.tone} bank-feed__message`}
          role="status"
          data-testid="bank-feed-message"
        >
          {message.text}
        </p>
      ) : null}
      {choosing ? (
        <ChooseBankDialog
          administrationId={administrationId}
          account={account}
          onClose={() => setChoosing(false)}
        />
      ) : null}
    </div>
  );
}

function initials(name: string): string {
  const words = name
    .replace(/[^\p{L}\p{N} ]/gu, " ")
    .split(/\s+/)
    .filter(Boolean);
  return (
    words.length > 1 ? `${words[0]?.[0] ?? ""}${words[1]?.[0] ?? ""}` : name.slice(0, 2)
  ).toUpperCase();
}

/**
 * Pick the bank. Choosing one starts the consent and sends the whole page to the bank's own
 * site: a full navigation, never a frame, because a bank's login must be on the bank's address.
 */
function ChooseBankDialog({
  administrationId,
  account,
  onClose,
}: {
  administrationId: string;
  account: BankAccountView;
  onClose: () => void;
}) {
  const { t, language } = useI18n();
  const { bank } = useServices();
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);
  const [institutions, setInstitutions] = useState<readonly BankFeedInstitutionView[] | null>(null);
  const [query, setQuery] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && opening === null) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, opening]);

  useEffect(() => {
    let cancelled = false;
    bank
      .listFeedInstitutions(administrationId, "NL")
      .then((result) => {
        if (!cancelled) setInstitutions(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [bank, administrationId]);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (institutions ?? []).filter(
      (item) => needle === "" || item.name.toLowerCase().includes(needle),
    );
  }, [institutions, query]);

  const choose = async (institution: BankFeedInstitutionView) => {
    setOpening(institution.id);
    setProblem(null);
    try {
      const started = await bank.connectFeed(
        administrationId,
        account.id,
        { id: institution.id, name: institution.name },
        language === "en" ? "en" : "nl",
      );
      window.location.assign(started.link);
    } catch (error) {
      setProblem(describeError(error));
      setOpening(null);
    }
  };

  return (
    <div className="dialog-backdrop bank-dialog-backdrop">
      <div
        ref={dialogRef}
        className="dialog bank-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="bank-feed-dialog-title"
        data-testid="bank-feed-dialog"
      >
        <div>
          <h2 id="bank-feed-dialog-title" className="bank-dialog__title">
            {t("bank.feed.choose_title")}
          </h2>
          <p className="bank-dialog__intro">{t("bank.feed.choose_intro")}</p>
        </div>
        {problem !== null ? <ErrorState message={problem} /> : null}
        {/* Searching needs a list: when the banks could not be loaded there is nothing to search. */}
        {institutions !== null ? (
          <label className="form__field">
            <span>{t("bank.feed.search")}</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              autoComplete="off"
              data-testid="bank-feed-search"
            />
          </label>
        ) : null}
        {institutions === null && problem === null ? (
          <div role="status">
            <span className="ledgr-visually-hidden">{t("bank.feed.loading_banks")}</span>
            <LoadingSkeleton rows={4} />
          </div>
        ) : null}
        {institutions !== null ? (
          shown.length === 0 ? (
            <p className="bank-feed__hint">{t("bank.feed.no_results")}</p>
          ) : (
            <ul className="bank-feed__banks" data-testid="bank-feed-banks">
              {shown.map((institution) => (
                <li key={institution.id}>
                  <button
                    type="button"
                    className="bank-feed__bank"
                    disabled={opening !== null}
                    onClick={() => void choose(institution)}
                    data-testid={`bank-feed-bank-${institution.id}`}
                  >
                    <span className="bank-feed__bank-mark" aria-hidden="true">
                      {initials(institution.name)}
                    </span>
                    <span className="bank-feed__bank-name">{institution.name}</span>
                    {opening === institution.id ? (
                      <span className="bank-feed__meta">{t("bank.feed.redirecting")}</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : null}
        <div className="dialog__actions">
          <button
            type="button"
            className="button--quiet"
            onClick={onClose}
            disabled={opening !== null}
          >
            {t("bank.action.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * `/bank/feed-return?ref=<connection>`: where the bank sends the person back. The consent is
 * checked (and the first lines read) once, then the Bank screen opens with a one-line notice.
 */
export function BankFeedReturn() {
  const { t } = useI18n();
  const { bank } = useServices();
  const { administration } = useAdministration();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const reference = params.get("ref");
  const [problem, setProblem] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    // Once, even under StrictMode's double effect: completing is idempotent on the server, but a
    // second call would only repeat the first read for nothing.
    if (started.current) return;
    started.current = true;
    if (reference === null) {
      navigate("/bank", { replace: true });
      return;
    }
    bank
      .completeFeed(administration.id, reference)
      .then((connection) => {
        const feedNotice: FeedNotice =
          connection.status === "linked"
            ? { kind: "connected", bank: connection.institution_name ?? "" }
            : connection.status === "pending"
              ? { kind: "pending" }
              : { kind: "failed", reason: connection.last_error ?? "bank_feed_consent_failed" };
        navigate("/bank", { replace: true, state: { feedNotice } });
      })
      .catch((error: unknown) => setProblem(describeError(error)));
  }, [bank, administration.id, reference, navigate]);

  return (
    <section className="screen" data-testid="bank-feed-return">
      {problem === null ? (
        <p role="status" className="bank-feed__hint">
          {t("bank.feed.returning")}
        </p>
      ) : (
        <>
          <ErrorState message={problem} />
          <p>
            <button
              type="button"
              className="button-link"
              onClick={() => navigate("/bank", { replace: true })}
            >
              {t("bank.feed.back_to_bank")}
            </button>
          </p>
        </>
      )}
    </section>
  );
}
