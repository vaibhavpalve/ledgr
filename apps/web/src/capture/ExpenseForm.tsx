import { useCallback, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import { PAYMENT_METHODS, VAT_TREATMENTS } from "@ledgr/shared-types";
import type { ExpenseView, PaymentMethod, VatTreatment } from "@ledgr/shared-types";

import type { CaptureApi, ExpenseFormPatch } from "./api";

/**
 * FR-EXP-001b's form, with FR-EXP-001e's payment method and FR-EXP-001g's
 * warnings.
 *
 *     FR-EXP-001b  The expense form asks for the minimum — date, supplier,
 *                  gross amount, VAT rate, category — with VAT and net
 *                  calculated automatically and the category defaulting from
 *                  the user's history. Everything else is optional and hidden
 *                  by default.
 *
 * --- Six fields, and the rest is derived ---
 *
 * The gross amount is asked for because it is the figure PRINTED on the
 * receipt: the one a person can read off without doing arithmetic. VAT, net
 * and the rate are computed by the server from the treatment and the date
 * (CMP-014) and are shown here read-only. There is no input for them, and that
 * is deliberate rather than economical — accepting one would let this screen
 * assert a figure the database is about to disagree with.
 *
 * --- Nothing here blocks ---
 *
 * FR-EXP-001c: "the product never blocks on extraction being available." Save
 * accepts any subset. `missing_fields` is shown as information — what is still
 * needed before SUBMITTING — rather than as a set of validation errors on a
 * form somebody has only started.
 *
 * FR-EXP-001g's duplicate warnings arrive alongside `can_be_marked_ready:
 * true` on purpose, and this screen honours that: the submit button is never
 * gated on the warning list being empty, because two identical receipts can be
 * legitimate.
 *
 * --- Money is a string from end to end (NFR-031) ---
 *
 * `gross_amount` is held as the typed string and sent as a string. It is never
 * put through `Number()` or `parseFloat`: JSON has one number type and it is a
 * double, so a round trip through one loses the value before the server can
 * see it. The rendered figures go through `money()`, which takes a decimal
 * string for the same reason.
 */
export function ExpenseForm({
  administrationId,
  expense,
  api,
  onChanged,
}: {
  administrationId: string;
  expense: ExpenseView;
  api: CaptureApi;
  /** Called with the server's answer after every successful write. */
  onChanged?: (next: ExpenseView) => void;
}) {
  const { t, money, date } = useI18n();
  const [draft, setDraft] = useState<ExpenseFormPatch>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const value = <K extends keyof ExpenseFormPatch>(field: K): string => {
    const pending = draft[field];
    if (pending !== undefined) return pending === null ? "" : String(pending);
    const stored = expense[field as keyof ExpenseView];
    return stored === null || stored === undefined ? "" : String(stored);
  };

  const edit = (patch: ExpenseFormPatch) => {
    setDraft((current) => ({ ...current, ...patch }));
    setSaved(false);
  };

  const write = useCallback(
    async (action: "save" | "submit") => {
      setSaving(true);
      setProblem(null);
      try {
        // Only what was touched. PATCH rather than PUT is load-bearing on the
        // server too: an omitted field must not mean "clear it", or saving one
        // changed field would wipe the other five.
        const afterSave =
          Object.keys(draft).length > 0
            ? await api.updateExpense(administrationId, expense.id, draft)
            : expense;
        const next =
          action === "submit" ? await api.markReady(administrationId, expense.id) : afterSave;
        setDraft({});
        setSaved(true);
        onChanged?.(next);
      } catch (error) {
        // The API's sentence, already translated into the reader's language by
        // the server (FR-UX-007). Shown rather than replaced, because it names
        // the specific refusal and this screen would only be guessing.
        setProblem(error instanceof Error ? error.message : "");
      } finally {
        setSaving(false);
      }
    },
    [administrationId, api, draft, expense, onChanged],
  );

  return (
    <form
      className="expense-form"
      aria-label={t("capture.form.title")}
      onSubmit={(event) => {
        event.preventDefault();
        void write("save");
      }}
    >
      <h2>{t("capture.form.title")}</h2>

      <label>
        {t("capture.form.date")}
        <input
          type="date"
          data-testid="expense-date"
          value={value("expense_date")}
          onChange={(event) => edit({ expense_date: event.target.value || null })}
        />
      </label>

      <label>
        {t("capture.form.supplier")}
        <input
          type="text"
          data-testid="expense-supplier"
          value={value("supplier")}
          onChange={(event) => edit({ supplier: event.target.value || null })}
        />
      </label>

      <label>
        {t("capture.form.gross_amount")}
        {/*
          `inputMode="decimal"` rather than `type="number"`: a number input
          hands back a value the browser has already parsed as a double, and
          NFR-031 runs through the client too. This stays the string the person
          typed, all the way to the wire.
        */}
        <input
          type="text"
          inputMode="decimal"
          data-testid="expense-gross-amount"
          value={value("gross_amount")}
          onChange={(event) => edit({ gross_amount: event.target.value || null })}
        />
      </label>

      <label>
        {t("capture.form.vat_treatment")}
        <select
          data-testid="expense-vat-treatment"
          value={value("vat_treatment")}
          onChange={(event) =>
            edit({ vat_treatment: (event.target.value || null) as VatTreatment | null })
          }
        >
          <option value="" />
          {VAT_TREATMENTS.map((treatment) => (
            <option key={treatment} value={treatment}>
              {t(`capture.vat.${treatment}`)}
            </option>
          ))}
        </select>
      </label>

      <label>
        {t("capture.form.payment_method")}
        <select
          data-testid="expense-payment-method"
          value={value("payment_method")}
          onChange={(event) =>
            edit({ payment_method: (event.target.value || null) as PaymentMethod | null })
          }
        >
          <option value="" />
          {PAYMENT_METHODS.map((method) => (
            <option key={method} value={method}>
              {t(`capture.payment.${method}`)}
            </option>
          ))}
        </select>
      </label>

      <label>
        {t("capture.form.category")}
        <input
          type="text"
          data-testid="expense-category"
          value={value("category")}
          onChange={(event) => edit({ category: event.target.value || null })}
        />
      </label>

      {/*
        FR-EXP-001b's "defaulting from the user's history", offered rather than
        applied. The API sends null once a category has been chosen, so this
        cannot reappear to argue with a decision already made.
      */}
      {expense.suggested_category !== null ? (
        <p data-testid="expense-suggestion">
          {t("capture.form.suggested_category", { category: expense.suggested_category })}
          <button
            type="button"
            data-testid="expense-use-suggestion"
            onClick={() => edit({ category: expense.suggested_category })}
          >
            {t("capture.form.use_suggestion")}
          </button>
        </p>
      ) : null}

      {/* Computed, shown, never accepted. */}
      {expense.vat_amount !== null && expense.net_amount !== null && expense.vat_rate !== null ? (
        <p data-testid="expense-derived">
          {t("capture.form.derived", {
            vat: money(expense.vat_amount),
            net: money(expense.net_amount),
            rate: expense.vat_rate,
          })}
        </p>
      ) : null}

      {expense.missing_fields.length > 0 ? (
        <p data-testid="expense-missing">
          {t("capture.form.still_needed", {
            fields: expense.missing_fields
              .map((field) => t(`capture.form.field.${field}`))
              .join(", "),
          })}
        </p>
      ) : null}

      {expense.duplicate_warnings.length > 0 ? (
        <section aria-label={t("capture.duplicate.title")} data-testid="expense-duplicates">
          <h3>{t("capture.duplicate.title")}</h3>
          <ul>
            {expense.duplicate_warnings.map((warning) => (
              <li key={warning.expense_id} data-testid="expense-duplicate">
                <span>
                  {t("capture.duplicate.entry", {
                    supplier: warning.supplier,
                    date: date(warning.expense_date),
                    amount: money(warning.gross_amount),
                  })}
                </span>
                <span>
                  {warning.same_submitter
                    ? t("capture.duplicate.same_submitter")
                    : t("capture.duplicate.other_submitter")}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {problem !== null ? (
        <p role="alert" data-testid="expense-problem">
          {problem}
        </p>
      ) : null}

      {saved ? (
        <p role="status" data-testid="expense-saved">
          {t("capture.form.saved")}
        </p>
      ) : null}

      <button type="submit" disabled={saving} data-testid="expense-save">
        {saving ? t("capture.form.saving") : t("capture.form.save")}
      </button>

      {/*
        Disabled only on what the SERVER says is missing — never on the
        duplicate warnings, which warn and do not block (FR-EXP-001g).
      */}
      <button
        type="button"
        disabled={saving || !expense.can_be_marked_ready}
        data-testid="expense-submit"
        onClick={() => void write("submit")}
      >
        {t("capture.form.mark_ready")}
      </button>
    </form>
  );
}
