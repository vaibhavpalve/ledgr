import { toDecimalInput } from "../ui/decimal";

/**
 * A trial balance exported from the previous package (Moneybird, Exact Online, e-Boekhouden,
 * Twinfield, a spreadsheet), read into opening lines by account CODE.
 *
 * Every package names its columns differently, so the header is matched on meaning rather than a
 * fixed layout: a code column (`code`, `rekening`, `grootboekrekening`, `nummer`, `account`, ...),
 * and either separate debit and credit columns or one signed balance (`saldo`, `balance`,
 * `bedrag`) where positive is debit. The delimiter is whichever of `;`, `,` or tab the header uses
 * most - Dutch Excel writes `;`. Amounts go through `toDecimalInput`, so `1.234,56` reads.
 *
 * Nothing is guessed about accounts: a row whose code is not in the chart is reported back, not
 * posted somewhere else.
 */
export interface ImportedLine {
  readonly code: string;
  readonly debit: string;
  readonly credit: string;
}

export interface ImportResult {
  readonly lines: readonly ImportedLine[];
  /** Codes that did not parse into an amount, for the person to look at. */
  readonly unreadable: readonly string[];
}

const CODE =
  /^(code|rekening|rekeningnummer|grootboek|grootboekrekening|grootboeknummer|nummer|nr|account|account ?code|gl ?account)$/i;
const DEBIT = /^(debet|debit|dr)$/i;
const CREDIT = /^(credit|cr)$/i;
const BALANCE = /^(saldo|balans|balance|bedrag|amount|eindsaldo|saldo eind)$/i;

function split(line: string, delimiter: string): string[] {
  // Quoted fields may contain the delimiter ("1.234,56" in a comma-separated file).
  const cells: string[] = [];
  let current = "";
  let quoted = false;
  for (const char of line) {
    if (char === '"') quoted = !quoted;
    else if (char === delimiter && !quoted) {
      cells.push(current.trim());
      current = "";
    } else current += char;
  }
  cells.push(current.trim());
  return cells;
}

function delimiterOf(header: string): string {
  const counts = [";", ",", "\t"].map((d) => [d, header.split(d).length] as const);
  counts.sort((a, b) => b[1] - a[1]);
  return counts[0]?.[0] ?? ";";
}

function isAmount(value: string): boolean {
  return /^-?\d+(\.\d+)?$/.test(value);
}

export function readTrialBalance(text: string, language: string): ImportResult | null {
  // A byte-order mark (Excel's UTF-8 CSV) would otherwise glue itself to the first header.
  const body = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const rows = body
    .split(/\r?\n/)
    .filter((row) => row.trim() !== "");
  const header = rows[0];
  if (header === undefined) return null;
  const delimiter = delimiterOf(header);
  const names = split(header, delimiter).map((name) => name.replace(/"/g, "").trim());
  const find = (pattern: RegExp) => names.findIndex((name) => pattern.test(name));
  const code = find(CODE);
  const debit = find(DEBIT);
  const credit = find(CREDIT);
  const balance = find(BALANCE);
  if (code < 0 || ((debit < 0 || credit < 0) && balance < 0)) return null;

  const lines: ImportedLine[] = [];
  const unreadable: string[] = [];
  for (const row of rows.slice(1)) {
    const cells = split(row, delimiter);
    const accountCode = (cells[code] ?? "").replace(/"/g, "").trim();
    if (accountCode === "") continue;
    let dr = "0";
    let cr = "0";
    if (debit >= 0 && credit >= 0) {
      dr = toDecimalInput(cells[debit] ?? "", language) || "0";
      cr = toDecimalInput(cells[credit] ?? "", language) || "0";
    } else {
      const signed = toDecimalInput(cells[balance] ?? "", language) || "0";
      if (signed.startsWith("-")) cr = signed.slice(1);
      else dr = signed;
    }
    if (!isAmount(dr) || !isAmount(cr)) {
      unreadable.push(accountCode);
      continue;
    }
    lines.push({ code: accountCode, debit: dr, credit: cr });
  }
  return { lines, unreadable };
}
