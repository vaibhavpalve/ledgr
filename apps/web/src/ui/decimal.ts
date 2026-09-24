/**
 * What a person typed into an amount or quantity field, as the decimal string the API accepts.
 *
 * The API takes `"1234.56"` (NFR-031: a string, never a float). A Dutch reader types `1234,56`,
 * `1.234,56` or `€ 95,00`, and before this every one of those was sent as typed and refused - the
 * most common input in a bookkeeping product failed for the product's own market.
 *
 * Only separators and decoration change here; digits are never added, dropped or rounded, and a
 * string that is not a number stays exactly as typed so the server's own refusal names it.
 *
 *   - spaces, non-breaking spaces and a leading `€` are dropped;
 *   - with both `.` and `,`, the LAST one is the decimal separator and the other groups thousands
 *     (`1.234,56` and `1,234.56` both become `1234.56`);
 *   - a lone `,` is the decimal separator (`95,5` -> `95.5`);
 *   - a lone `.` is a decimal point, except in Dutch where one followed by exactly three digits
 *     groups thousands (`1.250` is twelve hundred and fifty to a Dutch reader, and `1.5` is not
 *     ambiguous to anyone).
 */
export function toDecimalInput(typed: string, language: string = "nl"): string {
  const compact = typed.replace(/\s/g, "").replace(/^€/, "");
  if (compact === "") return "";
  if (!/^[-+]?[0-9.,]+$/.test(compact)) return typed.trim();

  const lastDot = compact.lastIndexOf(".");
  const lastComma = compact.lastIndexOf(",");
  if (lastDot >= 0 && lastComma >= 0) {
    const decimal = lastDot > lastComma ? "." : ",";
    const grouping = decimal === "." ? "," : ".";
    return compact.split(grouping).join("").replace(decimal, ".");
  }
  if (lastComma >= 0) {
    return compact.indexOf(",") === lastComma ? compact.replace(",", ".") : typed.trim();
  }
  if (lastDot >= 0 && language === "nl") {
    const groups = compact.replace(/^[-+]/, "").split(".");
    const thousands = groups.length > 1 && groups.slice(1).every((group) => group.length === 3);
    if (thousands) return compact.split(".").join("");
  }
  return compact;
}
