/**
 * The product's icon set, on the contract every icon in this app already
 * follows (see `MobileShell`'s former `TabIcon` and `ThemeToggle`'s
 * `ThemeIcon`): one 20-unit grid, round caps and joins, `currentColor`, and a
 * stroke that paints 1.5px at the 20px size these render at. The glyphs are
 * the design canvas's own (`.design/Main.dc.html`'s rail), traced rather
 * than redrawn, so the shell and the artboard show the same marks.
 *
 * Inline rather than an icon package: a dozen glyphs do not justify a
 * dependency, and `currentColor` means they follow state and theme with no
 * extra rule. Every one is `aria-hidden`: the label beside it is the name.
 */
export type IconName =
  | "home"
  | "capture"
  | "approve"
  | "view"
  | "invoice"
  | "customers"
  | "ledger"
  | "clients"
  | "settings"
  | "search"
  | "chevron-down"
  | "chevron-right"
  | "arrow-right"
  | "reverse"
  | "lock"
  | "check"
  | "close"
  | "plus"
  | "mail"
  | "warning";

const PATHS: Record<IconName, string> = {
  home: "M3 9.5 10 3.5l7 6 M5 8.5V16a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V8.5",
  capture:
    "M3 7.5h2.8l1.4-2h5.6l1.4 2H17a1 1 0 0 1 1 1V15a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V8.5a1 1 0 0 1 1-1z M12.8 11a2.8 2.8 0 1 1-5.6 0 2.8 2.8 0 0 1 5.6 0z",
  approve: "M17.3 10a7.3 7.3 0 1 1-14.6 0 7.3 7.3 0 0 1 14.6 0z m-10.7.2 2.4 2.4 4.5-5",
  view: "M7.5 5.5h9M7.5 10h9M7.5 14.5h6 M3.8 5.5h.01M3.8 10h.01M3.8 14.5h.01",
  invoice:
    "M5 2.8h6l4 4v10.4a.8.8 0 0 1-.8.8H5a.8.8 0 0 1-.8-.8V3.6a.8.8 0 0 1 .8-.8z M11 2.8v4h4 M7 11.5h6M7 14.3h4",
  customers: "M13 6.2a3 3 0 1 1-6 0 3 3 0 0 1 6 0z M3.5 16.5a6.5 6.5 0 0 1 13 0",
  ledger: "M4 4.4A1.4 1.4 0 0 1 5.4 3H16v14H5.4A1.4 1.4 0 0 1 4 15.6z M4 14h12 M7.5 6.5h5",
  clients: "M4 10h12M11.6 5.6 16 10l-4.4 4.4 M8.4 5.6 4 10l4.4 4.4",
  settings:
    "M12.4 10a2.4 2.4 0 1 1-4.8 0 2.4 2.4 0 0 1 4.8 0z M10 2.6v2.2M10 15.2v2.2M17.4 10h-2.2M4.8 10H2.6M15.23 4.77l-1.56 1.56M6.33 13.67l-1.56 1.56M15.23 15.23l-1.56-1.56M6.33 6.33 4.77 4.77",
  search: "M14.4 9a5.4 5.4 0 1 1-10.8 0 5.4 5.4 0 0 1 10.8 0z m-1.2 4.2 3.2 3.2",
  "chevron-down": "m5.5 8 4.5 4.5L14.5 8",
  "chevron-right": "m8 5.5 4.5 4.5L8 14.5",
  "arrow-right": "M4 10h12M11.6 5.6 16 10l-4.4 4.4",
  reverse: "M4.4 9.6h7.8a3.6 3.6 0 0 1 0 7.2H9.4 m-2-10.6-3 3.4 3 3.4",
  lock: "M4.6 8.8h10.8a1.8 1.8 0 0 1 1.8 1.8v3.8a1.8 1.8 0 0 1-1.8 1.8H4.6a1.8 1.8 0 0 1-1.8-1.8v-3.8a1.8 1.8 0 0 1 1.8-1.8z M7.2 8.8V6.6a2.8 2.8 0 0 1 5.6 0v2.2",
  check: "m4.5 10.4 3.4 3.4 7.6-8",
  close: "M5.5 5.5 14.5 14.5M14.5 5.5 5.5 14.5",
  plus: "M10 4v12M4 10h12",
  mail: "M3 5.5h14v9H3z m0 0 7 5 7-5",
  warning: "M10 3.4 17.6 16.6H2.4z M10 8.2v3.4 M10 14.2h.01",
};

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={size <= 16 ? 1.88 : 1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
