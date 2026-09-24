/**
 * Whether the signed-in person books a purchase themselves (FR-EXP-001e).
 *
 * Presentation only (CLAUDE.md rule 3): the server decides, on Appendix A's "Post journal
 * entries", which Owner, Accountant and Bookkeeper hold and an Expense Submitter or an Approver
 * does not. This only chooses whether the screen OFFERS the booking step - so a submitter is told
 * who books it rather than being shown a button that is refused.
 */
const ROLES_THAT_BOOK: ReadonlySet<string> = new Set(["Owner", "Accountant", "Bookkeeper"]);

export function canBook(role: string): boolean {
  return ROLES_THAT_BOOK.has(role);
}
