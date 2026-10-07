/**
 * IBAN handling for the bank-account form: what a person types, what is shown, and whether it can
 * be right before anything is sent. The server stays the authority; this only catches typos early.
 */

/** "nl91 abna 0417164300" -> "NL91ABNA0417164300": what is stored and sent. */
export function normaliseIban(input: string): string {
  return input.replace(/[\s-]+/g, "").toUpperCase();
}

/** "NL91ABNA0417164300" -> "NL91 ABNA 0417 1643 00": groups of four, as banks print it. */
export function formatIban(input: string): string {
  return normaliseIban(input).replace(/(.{4})(?=.)/g, "$1 ");
}

/**
 * ISO 13616: two letters, two check digits, then up to 30 letters and digits; moved to the end and
 * read as a number, it leaves 1 modulo 97. The remainder is taken a few digits at a time, so the
 * number never has to fit in a JavaScript number. Dutch IBANs are always 18 characters.
 */
export function isValidIban(input: string): boolean {
  const iban = normaliseIban(input);
  if (!/^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$/.test(iban)) return false;
  if (iban.startsWith("NL") && iban.length !== 18) return false;
  const rearranged = iban.slice(4) + iban.slice(0, 4);
  const digits = rearranged.replace(/[A-Z]/g, (letter) => String(letter.charCodeAt(0) - 55));
  let remainder = 0;
  for (let index = 0; index < digits.length; index += 7) {
    remainder = Number(`${remainder}${digits.slice(index, index + 7)}`) % 97;
  }
  return remainder === 1;
}
