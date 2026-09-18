import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { SUPPORTED_LANGUAGES, useI18n } from "@ledgr/i18n";
import {
  COUNTRY_CODES,
  DELIVERY_CHANNELS,
  type CountryCode,
  type CustomerView,
  type DeliveryChannel,
} from "@ledgr/shared-types";

import { ApiError, describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { vatNumberFormatProblem, type CustomerBody } from "./api";

/**
 * `/customers/new` and `/customers/:id/edit` — one form, `PUT` whole on
 * edit (`api.customers.routes.update_customer`: a customer is edited as one
 * form and sent back whole).
 *
 * --- VAT-number feedback, in two layers ---
 *
 * While typing: the FORMAT (`vatNumberFormatProblem`) — a Dutch number has
 * one shape, and a typo is cheaper to catch before a save than after. On
 * save: the server consults VIES and answers with `vat_number_status`
 * alongside the number; the detail screen shows that verdict, and offers
 * the on-demand re-check. This form never claims a number is VALID: only
 * VIES can.
 *
 * `credit_limit` is kept and sent as the typed string (NFR-031). A 422
 * naming a `field` lands on that input (`aria-invalid` + the server's own
 * sentence), never in a banner.
 *
 * `?return=` is the create-inline path from the invoice form: on success
 * the person goes back where they came from.
 */
interface Draft {
  name: string;
  trade_name: string;
  address_line1: string;
  address_line2: string;
  postal_code: string;
  city: string;
  country: string;
  kvk_number: string;
  vat_number: string;
  payment_terms_days: string;
  credit_limit: string;
  delivery_channel: DeliveryChannel;
  invoice_email: string;
  language: string;
  notes: string;
}

const EMPTY: Draft = {
  name: "",
  trade_name: "",
  address_line1: "",
  address_line2: "",
  postal_code: "",
  city: "",
  country: "NL",
  kvk_number: "",
  vat_number: "",
  payment_terms_days: "30",
  credit_limit: "",
  delivery_channel: "email",
  invoice_email: "",
  language: "nl",
  notes: "",
};

function draftOf(customer: CustomerView): Draft {
  return {
    name: customer.name,
    trade_name: customer.trade_name ?? "",
    address_line1: customer.address_line1 ?? "",
    address_line2: customer.address_line2 ?? "",
    postal_code: customer.postal_code ?? "",
    city: customer.city ?? "",
    country: customer.country,
    kvk_number: customer.kvk_number ?? "",
    vat_number: customer.vat_number ?? "",
    payment_terms_days: String(customer.payment_terms_days),
    credit_limit: customer.credit_limit ?? "",
    delivery_channel: customer.delivery_channel,
    invoice_email: customer.invoice_email ?? "",
    language: customer.language,
    notes: customer.notes ?? "",
  };
}

const orNull = (value: string): string | null => (value.trim() === "" ? null : value.trim());

export function bodyOf(draft: Draft): CustomerBody {
  return {
    name: draft.name.trim(),
    trade_name: orNull(draft.trade_name),
    address_line1: orNull(draft.address_line1),
    address_line2: orNull(draft.address_line2),
    postal_code: orNull(draft.postal_code),
    city: orNull(draft.city),
    country: draft.country.trim().toUpperCase() || "NL",
    kvk_number: orNull(draft.kvk_number),
    vat_number: orNull(draft.vat_number.replace(/[\s.-]/g, "").toUpperCase()),
    peppol_participant_id: null,
    // A count of days, not money: `Number` is fine here (NFR-031 is about amounts).
    payment_terms_days: Number.parseInt(draft.payment_terms_days, 10) || 0,
    credit_limit: orNull(draft.credit_limit.replace(",", ".")),
    delivery_channel: draft.delivery_channel,
    invoice_email: orNull(draft.invoice_email),
    language: draft.language,
    notes: orNull(draft.notes),
  };
}

export function CustomerFormScreen() {
  const { t, language } = useI18n();
  const navigate = useNavigate();
  const { customerId } = useParams();
  const [params] = useSearchParams();
  const returnTo = params.get("return");
  const { administration } = useAdministration();
  const { customers } = useServices();
  const editing = customerId !== undefined;

  const [draft, setDraft] = useState<Draft | null>(editing ? null : EMPTY);
  const [loadProblem, setLoadProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [problemField, setProblemField] = useState<string | null>(null);

  useEffect(() => {
    if (customerId === undefined) return;
    let cancelled = false;
    customers
      .getCustomer(administration.id, customerId)
      .then((customer) => {
        if (!cancelled) setDraft(draftOf(customer));
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [customers, administration.id, customerId]);

  const vatProblem = useMemo(
    () =>
      draft === null || draft.vat_number.trim() === ""
        ? null
        : vatNumberFormatProblem(draft.vat_number),
    [draft],
  );

  // Intl.DisplayNames, not a hand-translated catalogue entry per country:
  // it's a standard, ICU-backed browser API that resolves a code to its
  // name in whichever language the caller asks for, correctly - see
  // packages/shared-types/src/countries.ts for why hand-authoring 249
  // names × every supported language would itself be an accuracy risk for
  // data that feeds real invoices and VAT numbers. Sorted by the
  // LOCALISED name (not by code) so the list reads naturally either
  // language it's shown in.
  const countryOptions = useMemo(() => {
    const names = new Intl.DisplayNames([language], { type: "region" });
    return COUNTRY_CODES.map((code) => ({ code, name: names.of(code) ?? code })).sort((a, b) =>
      a.name.localeCompare(b.name, language),
    );
  }, [language]);

  if (loadProblem !== null) {
    return (
      <section className="screen" aria-label={t("customers.edit.title")}>
        <ErrorState message={loadProblem}>
          <Link to="/customers" className="button-link">
            {t("customers.detail.back")}
          </Link>
        </ErrorState>
      </section>
    );
  }
  if (draft === null) {
    return (
      <section className="screen" aria-label={t("customers.edit.title")}>
        <LoadingSkeleton rows={6} />
      </section>
    );
  }

  const update = (patch: Partial<Draft>) =>
    setDraft((current) => (current === null ? current : { ...current, ...patch }));

  const submit = async () => {
    if (vatProblem !== null || draft.name.trim() === "") return;
    setSaving(true);
    setProblem(null);
    setProblemField(null);
    try {
      const saved =
        customerId === undefined
          ? await customers.createCustomer(administration.id, bodyOf(draft))
          : await customers.updateCustomer(administration.id, customerId, bodyOf(draft));
      navigate(returnTo ?? `/customers/${encodeURIComponent(saved.id)}`, { replace: true });
    } catch (error) {
      if (error instanceof ApiError && typeof error.fields.field === "string") {
        setProblemField(error.fields.field);
      }
      setProblem(describeError(error));
      setSaving(false);
    }
  };

  const invalid = (field: string) => (problemField === field ? true : undefined);

  return (
    <section
      className="screen"
      aria-label={editing ? t("customers.edit.title") : t("customers.new.title")}
      data-testid="customer-form"
    >
      <PageHeader title={editing ? t("customers.edit.title") : t("customers.new.title")} />
      <form
        className="form panel panel__body"
        aria-label={editing ? t("customers.edit.title") : t("customers.new.title")}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="form__grid">
          <div className="form__field">
            <label htmlFor="cust-name">{t("customers.field.name")}</label>
            <input
              id="cust-name"
              type="text"
              required
              value={draft.name}
              aria-invalid={invalid("name")}
              data-testid="customer-name"
              onChange={(e) => update({ name: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-trade">{t("customers.field.trade_name")}</label>
            <input
              id="cust-trade"
              type="text"
              value={draft.trade_name}
              data-testid="customer-trade-name"
              onChange={(e) => update({ trade_name: e.target.value })}
            />
          </div>
          <div className="form__field form__span">
            <label htmlFor="cust-addr1">{t("customers.field.address_line1")}</label>
            <input
              id="cust-addr1"
              type="text"
              autoComplete="street-address"
              value={draft.address_line1}
              aria-invalid={invalid("address_line1")}
              data-testid="customer-address1"
              onChange={(e) => update({ address_line1: e.target.value })}
            />
            <p className="form__hint">{t("customers.field.address_hint")}</p>
          </div>
          <div className="form__field form__span">
            <label htmlFor="cust-addr2">{t("customers.field.address_line2")}</label>
            <input
              id="cust-addr2"
              type="text"
              value={draft.address_line2}
              data-testid="customer-address2"
              onChange={(e) => update({ address_line2: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-postal">{t("customers.field.postal_code")}</label>
            <input
              id="cust-postal"
              type="text"
              autoComplete="postal-code"
              value={draft.postal_code}
              aria-invalid={invalid("postal_code")}
              data-testid="customer-postal-code"
              onChange={(e) => update({ postal_code: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-city">{t("customers.field.city")}</label>
            <input
              id="cust-city"
              type="text"
              value={draft.city}
              aria-invalid={invalid("city")}
              data-testid="customer-city"
              onChange={(e) => update({ city: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-country">{t("customers.field.country")}</label>
            <select
              id="cust-country"
              value={draft.country}
              aria-invalid={invalid("country")}
              data-testid="customer-country"
              onChange={(e) => update({ country: e.target.value as CountryCode })}
            >
              {countryOptions.map(({ code, name }) => (
                <option key={code} value={code}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div className="form__field">
            <label htmlFor="cust-kvk">{t("customers.field.kvk_number")}</label>
            <input
              id="cust-kvk"
              type="text"
              inputMode="numeric"
              value={draft.kvk_number}
              aria-invalid={invalid("kvk_number")}
              data-testid="customer-kvk"
              onChange={(e) => update({ kvk_number: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-vat">{t("customers.field.vat_number")}</label>
            <input
              id="cust-vat"
              type="text"
              value={draft.vat_number}
              aria-invalid={vatProblem !== null || problemField === "vat_number" ? true : undefined}
              aria-describedby="cust-vat-hint"
              data-testid="customer-vat"
              onChange={(e) => update({ vat_number: e.target.value })}
            />
            <p
              id="cust-vat-hint"
              className={vatProblem !== null ? "field-error" : "form__hint"}
              data-testid="customer-vat-hint"
            >
              {vatProblem !== null
                ? t("customers.field.vat_number_malformed")
                : t("customers.field.vat_number_hint")}
            </p>
          </div>
          <div className="form__field">
            <label htmlFor="cust-email">{t("customers.field.invoice_email")}</label>
            <input
              id="cust-email"
              type="email"
              autoComplete="email"
              value={draft.invoice_email}
              aria-invalid={invalid("invoice_email")}
              data-testid="customer-email"
              onChange={(e) => update({ invoice_email: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-channel">{t("customers.field.delivery_channel")}</label>
            <select
              id="cust-channel"
              value={draft.delivery_channel}
              data-testid="customer-channel"
              onChange={(e) => update({ delivery_channel: e.target.value as DeliveryChannel })}
            >
              {DELIVERY_CHANNELS.map((channel) => (
                <option key={channel} value={channel}>
                  {t(`customers.delivery_channel.${channel}`)}
                </option>
              ))}
            </select>
          </div>
          <div className="form__field">
            <label htmlFor="cust-terms">{t("customers.field.payment_terms_days")}</label>
            <input
              id="cust-terms"
              type="number"
              min={0}
              max={365}
              value={draft.payment_terms_days}
              aria-invalid={invalid("payment_terms_days")}
              data-testid="customer-terms"
              onChange={(e) => update({ payment_terms_days: e.target.value })}
            />
          </div>
          <div className="form__field">
            <label htmlFor="cust-credit">{t("customers.field.credit_limit")}</label>
            <input
              id="cust-credit"
              type="text"
              inputMode="decimal"
              value={draft.credit_limit}
              aria-invalid={invalid("credit_limit")}
              data-testid="customer-credit-limit"
              onChange={(e) => update({ credit_limit: e.target.value })}
            />
            <p className="form__hint">{t("customers.field.credit_limit_hint")}</p>
          </div>
          <div className="form__field">
            <label htmlFor="cust-language">{t("customers.field.language")}</label>
            <select
              id="cust-language"
              value={draft.language}
              data-testid="customer-language"
              onChange={(e) => update({ language: e.target.value })}
            >
              {SUPPORTED_LANGUAGES.map((language) => (
                <option key={language} value={language}>
                  {t(`common.language.name.${language}`)}
                </option>
              ))}
            </select>
            <p className="form__hint">{t("customers.field.language_hint")}</p>
          </div>
          <div className="form__field form__span">
            <label htmlFor="cust-notes">{t("customers.field.notes")}</label>
            <textarea
              id="cust-notes"
              rows={3}
              value={draft.notes}
              data-testid="customer-notes"
              onChange={(e) => update({ notes: e.target.value })}
            />
          </div>
        </div>

        {problem !== null ? (
          <p role="alert" className="alert alert--attention" data-testid="customer-form-problem">
            {problem}
          </p>
        ) : null}

        <div className="form__actions">
          <button
            type="submit"
            disabled={saving || vatProblem !== null}
            data-testid="customer-save"
          >
            {saving ? t("common.action.saving") : t("common.action.save")}
          </button>
          <Link
            to={
              returnTo ??
              (customerId === undefined
                ? "/customers"
                : `/customers/${encodeURIComponent(customerId)}`)
            }
            className="button-link button-link--quiet"
          >
            {t("common.action.cancel")}
          </Link>
        </div>
      </form>
    </section>
  );
}
