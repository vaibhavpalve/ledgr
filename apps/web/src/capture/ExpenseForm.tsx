import { useCallback, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import { VAT_TREATMENTS } from "@ledgr/shared-types";
import type { ExpenseView, VatTreatment } from "@ledgr/shared-types";

import { toDecimalInput } from "../ui/decimal";
import type { CaptureApi, ExpenseFormPatch } from "./api";
import type { AsyncAction } from "./asyncActions";

/** Below this, a reading's own confidence in a field is not enough to trust it
 * unchecked. A number for the review screen to point at, not a rule the server
 * enforces: nothing is blocked on it. */
const CHECK_BELOW = 0.8;

/**
 * ADR-095: which sentence a failed reading gets. A busy provider is worth a
 * retry; a refused key or model is not, until an administrator fixes it. Any
 * other reason keeps the general sentence.
 */
const FAILED_SENTENCE: Readonly<Record<string, string>> = {
  provider_rate_limited: "capture.read.failed_busy",
  provider_error: "capture.read.failed_busy",
  provider_unreachable: "capture.read.failed_busy",
  timeout: "capture.read.failed_busy",
  provider_auth_failed: "capture.read.failed_setup",
  provider_rejected_request: "capture.read.failed_setup",
  credentials_missing: "capture.read.failed_setup",
  credentials_invalid: "capture.read.failed_setup",
  credentials_unreadable: "capture.read.failed_setup",
  token_refused: "capture.read.failed_setup",
};

function failedSentence(reason: string | null): string {
  return (reason !== null ? FAILED_SENTENCE[reason] : undefined) ?? "capture.read.failed";
}

/**
 * "Check this", beside a field the automatic reading wrote and was not sure of.
 *
 * Shown only for a field the reading actually filled (it is in `extraction.
 * fields`) - a field it left empty is not "uncertain", it is just empty. It
 * stays until the invoice is submitted: the reading's confidence is a fact about
 * the reading, not about what the person has since decided, so editing a field
 * does not pretend to have re-read it.
 */
function CheckHint({
  expense,
  field,
  testId,
}: {
  expense: ExpenseView;
  field: string;
  testId: string;
}) {
  const { t } = useI18n();
  const confidence =
    expense.extraction?.status === "done" ? expense.extraction.fields[field] : undefined;
  if (confidence === undefined || confidence >= CHECK_BELOW) return null;
  return (
    <span className="expense-form__check" data-testid={testId}>
      {t("capture.read.check")}
    </span>
  );
}

/**
 * FR-EXP-001b's form, with FR-EXP-001g's warnings.
 *
 *     FR-EXP-001b  The expense form asks for the minimum — date, supplier,
 *                  gross amount, VAT rate, category — with VAT and net
 *                  calculated automatically and the category defaulting from
 *                  the user's history. Everything else is optional and hidden
 *                  by default.
 *
 * FR-EXP-001e's payment method is not one of these fields here: this screen
 * is for company-funded purchases only, so every expense it creates is born
 * business_account (`add_item`'s INSERT default) rather than asked for. A
 * personally paid receipt is a reimbursement claim, a different process.
 *
 * --- Five fields, and the rest is derived ---
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
  onDiscard,
}: {
  administrationId: string;
  expense: ExpenseView;
  api: CaptureApi;
  /** Called with the server's answer after every successful write. */
  onChanged?: (next: ExpenseView) => void;
  /**
   * ADR-117: throws this invoice away. Supplied by a screen that knows where to
   * go afterwards; without it the blocked-duplicate note has no button, so the
   * form never offers an action it cannot finish. May reject, and the server's
   * own sentence is shown.
   */
  onDiscard?: AsyncAction;
}) {
  const { t, money, language } = useI18n();
  const [draft, setDraft] = useState<ExpenseFormPatch>({});
  const [saving, setSaving] = useState(false);
  const [reading, setReading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  const [discarding, setDiscarding] = useState(false);

  const discard = async () => {
    if (onDiscard === undefined) return;
    setDiscarding(true);
    setProblem(null);
    try {
      await onDiscard();
    } catch (error) {
      setProblem(
        error instanceof Error && error.message !== ""
          ? error.message
          : t("capture.duplicate.discard.failed"),
      );
      setConfirmingDiscard(false);
      setDiscarding(false);
    }
  };

  // ADR-101: the invoice number also matching is near-certain the same
  // document, and the one case that blocks submission (already reflected in
  // can_be_marked_ready). ADR-116 moved every duplicate WARNING to the upload;
  // what stays here is the reason for a disabled Submit, so it is never a
  // button that refuses without saying why.
  const confirmedDuplicate = expense.duplicate_warnings.some(
    (warning) => warning.invoice_number_match === "same",
  );

  const readAgain = useCallback(async () => {
    setReading(true);
    setProblem(null);
    try {
      const next = await api.readExpenseAgain(administrationId, expense.id);
      onChanged?.(next);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "");
    } finally {
      setReading(false);
    }
  }, [administrationId, api, expense.id, onChanged]);

  const value = <K extends keyof ExpenseFormPatch>(field: K): string => {
    const pending = draft[field];
    if (pending !== undefined) return pending === null ? "" : String(pending);
    const stored = expense[field as keyof ExpenseView];
    return stored === null || stored === undefined ? "" : String(stored);
  };

  const edit = (patch: ExpenseFormPatch) => {
    setDraft((current) => ({ ...current, ...patch }));
  };

  // Submit is the form's one action: it saves whatever was typed, then
  // releases the claim - a person never has to remember to save first.
  const submit = useCallback(async () => {
    if (!expense.can_be_marked_ready) return;
    setSaving(true);
    setProblem(null);
    try {
      // Only what was touched. PATCH rather than PUT is load-bearing on the
      // server too: an omitted field must not mean "clear it", or saving one
      // changed field would wipe the other five.
      // The gross amount as typed ("121,00") is sent as the API reads it ("121.00").
      const patch: ExpenseFormPatch =
        typeof draft.gross_amount === "string"
          ? { ...draft, gross_amount: toDecimalInput(draft.gross_amount, language) }
          : draft;
      if (Object.keys(patch).length > 0) {
        await api.updateExpense(administrationId, expense.id, patch);
      }
      const next = await api.markReady(administrationId, expense.id);
      setDraft({});
      onChanged?.(next);
    } catch (error) {
      // The API's sentence, already translated into the reader's language by
      // the server (FR-UX-007). Shown rather than replaced, because it names
      // the specific refusal and this screen would only be guessing.
      setProblem(error instanceof Error ? error.message : "");
    } finally {
      setSaving(false);
    }
  }, [administrationId, api, draft, expense, language, onChanged]);

  return (
    <form
      className="expense-form"
      aria-label={t("capture.form.title")}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      {/* A submitted invoice is not something to "complete": its facts are in the
          detail cards beside it, so the heading would only be wrong. */}
      {expense.status === "draft" ? <h2>{t("capture.form.title")}</h2> : null}

      {/*
        FR-AP-002: what automatic reading did, said once at the top. A reading is
        a suggestion, so a success asks for a check rather than claiming to be
        right; a failure says so, because a form that looks like nobody tried to
        fill it in reads as a bug.

        A certain VAT rate (21% or 9%) submits the claim without a person, so
        an expense that reaches this screen already `ready` has nothing left
        to check - showing the editable form under "check before you submit"
        would be wrong twice over: the fields cannot be saved anymore
        (`ExpenseFormService.update` refuses a ready claim), and there is
        nothing to check.
      */}
      {expense.status !== "draft" && expense.extraction?.status === "done" ? (
        <p className="expense-form__notice" data-testid="expense-read-submitted">
          {t("capture.read.submitted")}
        </p>
      ) : expense.extraction?.status === "done" ? (
        <p className="expense-form__notice" data-testid="expense-read-done">
          {t("capture.read.done")}
        </p>
      ) : null}
      {expense.status === "draft" && expense.extraction?.status === "failed" ? (
        <div
          className="expense-form__notice expense-form__notice--failed"
          data-testid="expense-read-failed"
          data-reason={expense.extraction.reason ?? undefined}
        >
          <p>{t(failedSentence(expense.extraction.reason))}</p>
          {expense.status === "draft" ? (
            <button
              type="button"
              className="expense-form__read-again"
              data-testid="expense-read-again"
              disabled={reading || saving}
              onClick={() => void readAgain()}
            >
              {reading ? t("capture.read.reading") : t("capture.read.again")}
            </button>
          ) : null}
        </div>
      ) : null}

      {expense.status === "draft" ? (
        <>
          <label>
            {t("capture.form.date")}
            <input
              type="date"
              data-testid="expense-date"
              value={value("expense_date")}
              onChange={(event) => edit({ expense_date: event.target.value || null })}
            />
            <CheckHint expense={expense} field="invoice_date" testId="expense-date-check" />
          </label>

          <label>
            {t("capture.form.supplier")}
            <input
              type="text"
              data-testid="expense-supplier"
              value={value("supplier")}
              onChange={(event) => edit({ supplier: event.target.value || null })}
            />
            <CheckHint expense={expense} field="supplier" testId="expense-supplier-check" />
          </label>

          <label>
            {t("capture.form.invoice_number")}
            <input
              type="text"
              data-testid="expense-invoice-number"
              value={value("invoice_number")}
              onChange={(event) => edit({ invoice_number: event.target.value || null })}
            />
            <CheckHint
              expense={expense}
              field="invoice_number"
              testId="expense-invoice-number-check"
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
            <CheckHint expense={expense} field="gross_amount" testId="expense-gross-amount-check" />
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
            <CheckHint expense={expense} field="vat_rate" testId="expense-vat-check" />
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
          {expense.vat_amount !== null &&
          expense.net_amount !== null &&
          expense.vat_rate !== null ? (
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

          {/*
            ADR-116: the duplicate WARNING is not here any more - it is said once,
            at the upload, where the person is still looking at the file. Only the
            one sentence a disabled Submit needs remains.
          */}
          {confirmedDuplicate ? (
            <div
              className="expense-form__notice expense-form__notice--attention expense-form__blocked"
              data-testid="expense-duplicate-blocked"
            >
              <p>{t("capture.duplicate.blocked")}</p>
              {/*
                ADR-117: the way out. Offered only where the screen can act on it,
                and asked twice because it removes the invoice from the list (its
                file is kept).
              */}
              {onDiscard !== undefined ? (
                confirmingDiscard ? (
                  <span className="expense-form__blocked-actions">
                    <span>{t("capture.duplicate.discard.confirm")}</span>
                    <button
                      type="button"
                      disabled={discarding}
                      data-testid="expense-discard-confirm"
                      onClick={() => void discard()}
                    >
                      {t("capture.duplicate.discard.yes")}
                    </button>
                    <button
                      type="button"
                      disabled={discarding}
                      data-testid="expense-discard-keep"
                      onClick={() => setConfirmingDiscard(false)}
                    >
                      {t("capture.duplicate.discard.keep")}
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="expense-form__discard"
                    data-testid="expense-discard"
                    onClick={() => setConfirmingDiscard(true)}
                  >
                    {t("capture.duplicate.discard.button")}
                  </button>
                )
              ) : null}
            </div>
          ) : null}

          {problem !== null ? (
            <p role="alert" data-testid="expense-problem">
              {problem}
            </p>
          ) : null}

          {/*
            Disabled only on what the SERVER says is missing — never on the
            duplicate warnings, which warn and do not block a person's own
            submit (FR-EXP-001g; only automatic submission skips a duplicate,
            see InvoiceExtractionService._submit_if_vat_is_certain).
          */}
          <button
            type="submit"
            disabled={saving || !expense.can_be_marked_ready}
            data-testid="expense-submit"
          >
            {saving ? t("capture.form.saving") : t("capture.form.mark_ready")}
          </button>
        </>
      ) : null}
    </form>
  );
}
