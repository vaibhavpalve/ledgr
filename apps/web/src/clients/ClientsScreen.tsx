import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { roleLabel, useI18n } from "@ledgr/i18n";
import type { AdministrationView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useSession } from "../session/SessionProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, PageHeader } from "../shell/ScreenState";

/**
 * `/clients` — FR-ONB-001b's firm portfolio: every client administration
 * the caller holds a grant on (the switcher's own list, from `GET /v1/me`),
 * as cards with the colour marker, name, KvK and role; a filter over that
 * list; and the switch itself (`PUT /v1/switcher/{id}`, then a fresh
 * `/v1/me`, then the dashboard).
 *
 * The filter is over data the caller ALREADY has — their own memberships —
 * so filtering it in the browser leaks nothing (the reasoning
 * `ClientSwitcher` gives for NOT filtering locally applies to a wider
 * search; this is the same list, narrowed).
 *
 * Empty (a new firm): one prominent "add your first client", opening the
 * same onboarding wizard a business uses (FR-ONB-001b).
 */
export function ClientsScreen() {
  const { t, language } = useI18n();
  const navigate = useNavigate();
  const { me, administrations, administration, switchAdministration } = useSession();
  const [query, setQuery] = useState("");
  const [switching, setSwitching] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (needle === "") return administrations;
    return administrations.filter(
      (entry) =>
        entry.legal_name.toLowerCase().includes(needle) ||
        (entry.trade_name ?? "").toLowerCase().includes(needle) ||
        (entry.kvk_number ?? "").includes(needle),
    );
  }, [administrations, query]);

  const open = async (entry: AdministrationView) => {
    setSwitching(entry.id);
    setProblem(null);
    try {
      if (entry.id !== administration?.id) await switchAdministration(entry.id);
      navigate("/");
    } catch (error) {
      setProblem(describeError(error));
      setSwitching(null);
    }
  };

  const isFirm = me.organization.kind === "firm";

  return (
    <section className="screen" aria-label={t("client.portfolio.title")} data-testid="clients">
      <PageHeader
        title={t("client.portfolio.title")}
        context={t("client.portfolio.count", { count: administrations.length })}
        action={
          administrations.length > 0 ? (
            <Link to="/onboarding" className="button-link button-link--primary" data-testid="clients-add">
              <Icon name="plus" size={18} />
              {isFirm ? t("client.portfolio.add") : t("client.portfolio.add_administration")}
            </Link>
          ) : undefined
        }
      />

      {problem !== null ? <ErrorState message={problem} /> : null}

      {administrations.length === 0 ? (
        <EmptyState
          icon={<Icon name="clients" size={32} />}
          title={isFirm ? t("client.portfolio.empty_title") : t("client.portfolio.empty_title_business")}
          body={isFirm ? t("client.portfolio.empty_body") : t("client.portfolio.empty_body_business")}
          action={
            <Link to="/onboarding" className="button-link button-link--primary" data-testid="clients-add-first">
              {isFirm ? t("client.portfolio.add_first") : t("client.portfolio.add_administration")}
            </Link>
          }
          testId="clients-empty"
        />
      ) : (
        <>
          <div className="form__field">
            <label htmlFor="clients-filter">{t("client.switcher.search_label")}</label>
            <input
              id="clients-filter"
              type="search"
              value={query}
              placeholder={t("client.switcher.search_placeholder")}
              data-testid="clients-filter"
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>

          {visible.length === 0 ? (
            <EmptyState
              title={t("client.switcher.no_matches", { query })}
              body={t("client.portfolio.no_matches_body")}
              action={
                <button type="button" onClick={() => setQuery("")}>
                  {t("customers.list.clear_search")}
                </button>
              }
              testId="clients-no-matches"
            />
          ) : (
            <ul className="card-grid" data-testid="clients-grid">
              {visible.map((entry) => {
                const current = entry.id === administration?.id;
                return (
                  <li key={entry.id} className="card" data-testid="client-card" data-administration-id={entry.id}>
                    <div className="card__head">
                      <span className={`client-marker client-marker--large client-marker--${entry.colour}`} aria-hidden="true">
                        {entry.initials}
                      </span>
                      <div>
                        <p className="card__title">{entry.trade_name ?? entry.legal_name}</p>
                        {entry.trade_name !== null ? <p className="card__meta">{entry.legal_name}</p> : null}
                      </div>
                    </div>
                    <p className="card__meta ledgr-num">
                      {entry.kvk_number === null ? t("client.portfolio.no_kvk") : t("client.switcher.kvk", { number: entry.kvk_number })}
                    </p>
                    <p className="card__meta">{roleLabel(entry.role, language, { isSystem: entry.role_is_system })}</p>
                    <div className="card__actions">
                      {current ? (
                        <span className="chip chip--accent" data-testid="client-current">
                          {t("client.switcher.current")}
                        </span>
                      ) : null}
                      <button
                        type="button"
                        className={current ? undefined : "button--primary"}
                        disabled={switching !== null}
                        data-testid={`client-open-${entry.id}`}
                        onClick={() => void open(entry)}
                      >
                        {switching === entry.id ? t("client.portfolio.opening") : t("client.portfolio.open")}
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
