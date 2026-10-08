import { BookOpen, ChartColumn, Check, House, Landmark, Percent, ShoppingCart } from "lucide-react";

import { Logo } from "../ui/Logo";
import { MOCK, SAMPLE } from "./content/mockups";
import { useL } from "./l10n";

/**
 * The product, drawn in HTML rather than shown as screenshots (the brief): sharp at every size and
 * in both languages. Each is fixed sample data, so each is `aria-hidden`; the text beside it says
 * what it shows.
 */

export function AppMockup() {
  const l = useL();
  const nav = [
    { icon: House, label: MOCK.nav.overview, on: true, badge: null },
    { icon: ShoppingCart, label: MOCK.nav.purchases, on: false, badge: "4" },
    { icon: Landmark, label: MOCK.nav.bank, on: false, badge: null },
    { icon: BookOpen, label: MOCK.nav.ledger, on: false, badge: null },
    { icon: Percent, label: MOCK.nav.vat, on: false, badge: null },
    { icon: ChartColumn, label: MOCK.nav.reports, on: false, badge: null },
  ];
  return (
    <div className="mk-frame" aria-hidden="true">
      <div className="mk-frame-bar">
        <i />
        <i />
        <i />
        <span>{MOCK.url}</span>
      </div>
      <div className="mk-app">
        <div className="mk-app-side">
          <Logo size={17} />
          {nav.map((item) => (
            <span key={item.label.en} className={`mk-app-nav${item.on ? " mk-app-nav--on" : ""}`}>
              <item.icon size={16} strokeWidth={1.75} />
              {l(item.label)}
              {item.badge !== null ? <span className="mk-n">{item.badge}</span> : null}
            </span>
          ))}
        </div>
        <div className="mk-app-main">
          <div className="mk-app-top">
            <b>{l(MOCK.nav.overview)}</b>
            <span className="mk-fake-btn">{l(MOCK.capture)}</span>
          </div>
          <div className="mk-app-grid">
            <div className="mk-tile mk-tile--forest">
              <small>{l(MOCK.cash)}</small>
              <div className="mk-fig">{MOCK.cashAmount}</div>
              <svg viewBox="0 0 300 50" width="100%" height="44" preserveAspectRatio="none">
                <path
                  d="M0 40 L30 36 L60 38 L90 28 L120 31 L150 22 L180 25 L210 15 L240 18 L270 9 L300 5"
                  fill="none"
                  stroke="var(--leaf)"
                  strokeWidth="2"
                  vectorEffect="non-scaling-stroke"
                />
              </svg>
            </div>
            <div className="mk-tile mk-tile--lemon">
              <small>{l(MOCK.vatDue)}</small>
              <div className="mk-fig">{MOCK.vatAmount}</div>
              <div className="mk-sub">{l(MOCK.vatBy)}</div>
            </div>
            <div className="mk-tile mk-tile--leaf">
              <small>{l(MOCK.toBook)}</small>
              <div className="mk-fig">{MOCK.toBookCount}</div>
              <div className="mk-sub">{l(MOCK.toBookSub)}</div>
            </div>
          </div>
          <div className="mk-app-row2">
            <div className="mk-tile">
              <b>{l(MOCK.todo)}</b>
              <div className="mk-todo">
                <span className="mk-chip mk-chip--review">{l(MOCK.review)}</span>
                {SAMPLE.vendor}
                <span className="mk-amt">{MOCK.receiptAmount}</span>
              </div>
              <div className="mk-todo">
                <span className="mk-chip">{l(MOCK.bankChip)}</span>
                {MOCK.todoBank}
                <span className="mk-amt">{MOCK.todoBankAmount}</span>
              </div>
              <div className="mk-todo">
                <span className="mk-chip mk-chip--done">
                  <Check size={12} strokeWidth={2} />
                  {l(MOCK.ready)}
                </span>
                {l(MOCK.todoVat)}
                <span className="mk-amt mk-muted">{l(MOCK.todoVatWhen)}</span>
              </div>
            </div>
            <div className="mk-tile">
              <b>{l(MOCK.profit)}</b>
              <div className="mk-fig">{MOCK.profitAmount}</div>
              <svg viewBox="0 0 220 60" width="100%" height="60">
                {[34, 28, 31, 22, 26, 16, 24, 19].map((y, index) => (
                  <rect
                    key={index}
                    x={index * 24}
                    y={y}
                    width="14"
                    height={60 - y}
                    rx="2"
                    fill="var(--forest)"
                  />
                ))}
                <rect x="192" y="8" width="14" height="52" rx="2" fill="var(--lemon)" />
              </svg>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ReceiptCard({ tilt = true }: { tilt?: boolean }) {
  const l = useL();
  return (
    <div className="mk-receipt" style={tilt ? { transform: "rotate(-3deg)" } : undefined}>
      <b>{SAMPLE.vendor}</b>
      <div>{SAMPLE.address}</div>
      <div>{SAMPLE.stamp}</div>
      <hr />
      <div className="mk-r">
        <span>{l(SAMPLE.item)}</span>
        <span>{SAMPLE.lines[0]?.debit}</span>
      </div>
      <div className="mk-r">
        <span>{SAMPLE.vatLine}</span>
        <span>{SAMPLE.lines[1]?.debit}</span>
      </div>
      <hr />
      <div className="mk-r">
        <b>{l(SAMPLE.receiptTotal)}</b>
        <b>{SAMPLE.total}</b>
      </div>
    </div>
  );
}

export function JournalEntryCard() {
  const l = useL();
  return (
    <div className="mk-doc">
      <div className="mk-doc-head">
        <b>{SAMPLE.vendor}</b>
        <span className="mk-chip mk-chip--review">{l(MOCK.detected)}</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>{l(MOCK.account)}</th>
            <th className="mk-r">{l(MOCK.debit)}</th>
            <th className="mk-r">{l(MOCK.credit)}</th>
          </tr>
        </thead>
        <tbody>
          {SAMPLE.lines.map((line) => (
            <tr key={line.account}>
              <td>{line.account}</td>
              <td className="mk-r">{line.debit}</td>
              <td className="mk-r">{line.credit}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>{l(SAMPLE.receiptTotal)}</td>
            <td className="mk-r">{SAMPLE.sum}</td>
            <td className="mk-r">{SAMPLE.sum}</td>
          </tr>
        </tfoot>
      </table>
      <div className="mk-doc-foot">
        <span className="mk-chip mk-chip--done">
          <Check size={12} strokeWidth={2} />
          {l(MOCK.balanced)}
        </span>
        <span>{l(MOCK.booked)}</span>
      </div>
    </div>
  );
}

/** The receipt and the entry it becomes. */
export function CaptureMedia() {
  return (
    <div className="mk-media" aria-hidden="true">
      <div className="mk-pair">
        <ReceiptCard />
        <div style={{ width: "min(100%, 360px)" }}>
          <JournalEntryCard />
        </div>
      </div>
    </div>
  );
}

export function BankMedia() {
  const l = useL();
  return (
    <div className="mk-media mk-media--sunken" aria-hidden="true">
      <div className="mk-stack">
        {MOCK.bankLines.map((line) => (
          <div
            key={line.name}
            className={`mk-bankline${line.attention ? " mk-bankline--attention" : ""}`}
          >
            <span
              className="mk-av"
              style={line.attention ? { background: "var(--lemon-tint)" } : undefined}
            >
              {line.initials}
            </span>
            <div>
              <b>{line.name}</b>
              <small>{l(line.note)}</small>
            </div>
            <span className={`mk-amt${line.incoming ? " mk-in" : ""}`}>{line.amount}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function BtwMedia() {
  const l = useL();
  return (
    <div className="mk-media mk-media--lemon" aria-hidden="true">
      <div className="mk-doc">
        <div className="mk-doc-head">
          <b>{l(MOCK.btwTitle)}</b>
          <span className="mk-chip mk-chip--done">
            <Check size={12} strokeWidth={2} />
            {l(MOCK.checked)}
          </span>
        </div>
        {MOCK.btwRows.map((row) => (
          <div key={row.label} className="mk-btw-row">
            <span>{row.label}</span>
            <span>{row.amount}</span>
          </div>
        ))}
        <div className="mk-btw-total">
          <span>{l(MOCK.toPay)}</span>
          <b>{MOCK.vatAmount}</b>
        </div>
      </div>
    </div>
  );
}

export function GrootboekMedia() {
  const l = useL();
  return (
    <div className="mk-media" aria-hidden="true">
      <div className="mk-stack">
        <JournalEntryCard />
        <div className="mk-doc">
          <div className="mk-doc-head">
            <b>{l(MOCK.journalTitle)}</b>
            <span className="mk-chip">{l(MOCK.reversal)}</span>
          </div>
          <table>
            <tbody>
              {SAMPLE.lines.map((line) => (
                <tr key={line.account}>
                  <td>{line.account}</td>
                  <td className="mk-r">{line.credit}</td>
                  <td className="mk-r">{line.debit}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mk-doc-foot">
            <span>{l(MOCK.linked)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function InvoiceMedia() {
  const l = useL();
  return (
    <div className="mk-media mk-media--sunken" aria-hidden="true">
      <div className="mk-doc">
        <div className="mk-doc-head">
          <b>{l(MOCK.invoiceTitle)}</b>
          <span className="mk-chip mk-chip--done">
            <Check size={12} strokeWidth={2} />
            {l(MOCK.paid)}
          </span>
        </div>
        <div className="mk-doc-meta">
          <span>{l(MOCK.invoiceTo)}</span>
          <b>{MOCK.invoiceCustomer}</b>
        </div>
        <table>
          <tbody>
            <tr>
              <td>{l(MOCK.invoiceLine)}</td>
              <td className="mk-r">{MOCK.invoiceNet}</td>
            </tr>
            <tr>
              <td>{SAMPLE.vatLine}</td>
              <td className="mk-r">{MOCK.invoiceVat}</td>
            </tr>
          </tbody>
          <tfoot>
            <tr>
              <td>{l(SAMPLE.receiptTotal)}</td>
              <td className="mk-r">{MOCK.invoiceTotal}</td>
            </tr>
          </tfoot>
        </table>
        <div className="mk-doc-foot">
          <span>{l(MOCK.invoiceFoot)}</span>
          <span>{l(MOCK.invoiceMatched)}</span>
        </div>
      </div>
    </div>
  );
}

export function ReportsMedia() {
  const l = useL();
  return (
    <div className="mk-media" aria-hidden="true">
      <div className="mk-doc">
        <div className="mk-doc-head">
          <b>{l(MOCK.pnlTitle)}</b>
          <span className="mk-chip">{l(MOCK.pnlPeriod)}</span>
        </div>
        <table>
          <tbody>
            {MOCK.pnlRows.map((row) => (
              <tr key={row.label}>
                <td>{row.label}</td>
                <td className="mk-r">{row.amount}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>{l(MOCK.result)}</td>
              <td className="mk-r">{MOCK.resultAmount}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

export function PhoneMedia() {
  const l = useL();
  return (
    <div className="mk-media mk-media--forest" aria-hidden="true">
      <div className="mk-phone">
        <div className="mk-phone-in">
          <div className="mk-phone-cam">
            <i className="mk-phone-corner mk-phone-corner--tl" />
            <i className="mk-phone-corner mk-phone-corner--tr" />
            <i className="mk-phone-corner mk-phone-corner--bl" />
            <i className="mk-phone-corner mk-phone-corner--br" />
            <ReceiptCard tilt={false} />
          </div>
          <div className="mk-phone-sheet">
            <div className="mk-phone-row">
              <span className="mk-chip mk-chip--done">{l(MOCK.phoneRead)}</span>
              <span className="mk-chip mk-chip--review">{SAMPLE.vatLine}</span>
            </div>
            <b>{SAMPLE.vendor}</b>
            <div className="mk-fig">{MOCK.receiptAmount}</div>
            <span className="mk-fake-btn">{l(MOCK.phoneBook)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ClientsMedia() {
  const l = useL();
  return (
    <div className="mk-media mk-media--sunken" aria-hidden="true">
      <div className="mk-stack">
        <div className="mk-phone-row">
          <b>{l(MOCK.clientsTitle)}</b>
          <span className="mk-muted">{l(MOCK.clientsCount)}</span>
        </div>
        {MOCK.clients.map((client) => (
          <div key={client.name} className={`mk-client${client.open ? " mk-client--open" : ""}`}>
            <span className="mk-marker" style={{ background: client.colour }}>
              {client.initials}
            </span>
            <div>
              <b>{client.name}</b>
              <small>
                {client.kvk} · {l(client.role)}
              </small>
            </div>
            {client.open ? <span className="mk-chip mk-chip--review">{l(MOCK.open)}</span> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

export type MediaKind =
  | "capture"
  | "bank"
  | "grootboek"
  | "btw"
  | "invoicing"
  | "reports"
  | "mobile"
  | "clients"
  | "review";

export function Media({ kind }: { kind: MediaKind }) {
  switch (kind) {
    case "capture":
    case "review":
      return <CaptureMedia />;
    case "bank":
      return <BankMedia />;
    case "grootboek":
      return <GrootboekMedia />;
    case "btw":
      return <BtwMedia />;
    case "invoicing":
      return <InvoiceMedia />;
    case "reports":
      return <ReportsMedia />;
    case "mobile":
      return <PhoneMedia />;
    case "clients":
      return <ClientsMedia />;
  }
}
