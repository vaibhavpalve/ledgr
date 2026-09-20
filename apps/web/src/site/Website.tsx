import { Check } from "lucide-react";
import { Link } from "react-router-dom";

import "./Website.css";
import { ButtonLink, ClientAvatar, Logo, type AvatarTone } from "../ui";
import { COPY } from "./copy";

/**
 * The public marketing page (design/reference/Website.png, ADR-080), at
 * `/welcome`. Every claim comes from the reference; the sample figures in the
 * mockups are illustration, not statistics. There is no pricing, no
 * testimonial and no customer logo, because there are none to show.
 *
 * The four floating mockups use fixed sample data, so they are `aria-hidden`.
 */
export function Website() {
  return (
    <div className="ui-root site">
      <header className="site__header">
        <Logo size={30} />
        <nav aria-label={"Main"} className="site__nav">
          <a href="#product">{COPY.nav.product}</a>
          <a href="#books">{COPY.nav.books}</a>
          <a href="#accountants">{COPY.nav.accountants}</a>
        </nav>
        <div className="site__auth">
          <Link className="site__signin" to="/login">
            {COPY.signIn}
          </Link>
          <ButtonLink to="/signup" variant="primary">
            {COPY.cta}
          </ButtonLink>
        </div>
      </header>

      <main>
        <section id="product" className="site__hero">
          <div className="site__hero-copy">
            <span className="ui-badge ui-badge--booked site__pill">{COPY.hero.badge}</span>
            <h1 className="site__h1">
              {COPY.hero.lead} <span className="site__mark">{COPY.hero.mark}</span>
            </h1>
            <p className="site__lead site__lead--hero">{COPY.hero.body}</p>
            <div className="site__row">
              <ButtonLink to="/signup" variant="primary" size="xl">
                {COPY.cta}
              </ButtonLink>
              <a className="ui-btn ui-btn--xl" href="#books">
                {COPY.hero.secondary}
              </a>
            </div>
          </div>
          <HeroPanel />
        </section>

        <section className="site__facts" aria-label={"Facts"}>
          {COPY.facts.map(([title, body]) => (
            <div key={title}>
              <div className="site__fact-title">{title}</div>
              <div className="site__fact-body">{body}</div>
            </div>
          ))}
        </section>

        <section className="site__feature">
          <PhoneMock />
          <Feature block={COPY.capture} />
        </section>

        <section id="books" className="site__feature site__feature--sunken">
          <Feature block={COPY.books} narrow />
          <JournalMock />
        </section>

        <section id="accountants" className="site__feature">
          <ClientsMock />
          <Feature block={COPY.accountants} />
        </section>

        <section className="site__band-wrap">
          <div className="site__band">
            <Rules />
            <h2 className="site__h2 site__band-title">{COPY.band}</h2>
            <ButtonLink to="/signup" size="xl" className="site__band-btn">
              {COPY.cta}
            </ButtonLink>
          </div>
        </section>
      </main>

      <footer className="site__footer">
        <div className="site__footer-brand">
          <Logo size={30} />
          <p>{COPY.footer.blurb}</p>
        </div>
        <div className="site__footer-cols">
          <FooterCol title={COPY.nav.product} items={COPY.footer.product} />
          <FooterCol title={"Company"} items={COPY.footer.company} />
        </div>
      </footer>
    </div>
  );
}

function Feature({
  block,
  narrow,
}: {
  block: { eyebrow: string; title: string; lead: string; ticks: readonly string[] };
  narrow?: boolean;
}) {
  return (
    <div className={`site__text${narrow ? " site__text--narrow" : ""}`}>
      <span className="site__eyebrow">{block.eyebrow}</span>
      <h2 className="site__h2">{block.title}</h2>
      <p className="site__lead">{block.lead}</p>
      <ul className="site__ticks">
        {block.ticks.map((tick) => (
          <li key={tick}>
            <Check size={20} strokeWidth={2.4} aria-hidden="true" />
            <span>{tick}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FooterCol({ title, items }: { title: string; items: readonly string[] }) {
  return (
    <div>
      <div className="site__footer-title">{title}</div>
      <ul className="site__footer-list">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function Rules() {
  return (
    <svg className="site__rules" width="100%" height="100%" fill="none" aria-hidden="true">
      <g stroke="var(--on-panel)" strokeOpacity="0.1">
        {[64, 128, 192, 256].map((y) => (
          <line key={y} x1="0" x2="100%" y1={y} y2={y} />
        ))}
      </g>
      <g stroke="var(--on-panel)" strokeOpacity="0.16" strokeWidth="1.5">
        <circle cx="100%" cy="0" r="150" />
        <circle cx="100%" cy="0" r="250" />
      </g>
      <circle cx="100%" cy="0" r="70" fill="var(--accent)" />
    </svg>
  );
}

const SAMPLE = {
  cash: "€ 24.310,45",
  receivables: "€ 12.850,00",
  btw: "€ 4.216,80",
};

function HeroPanel() {
  return (
    <div className="site__panel" aria-hidden="true">
      <svg className="site__panel-art" width="100%" height="100%" fill="none">
        <g stroke="var(--on-panel)" strokeOpacity="0.1">
          {[80, 160, 240, 320, 400, 480, 560].map((y) => (
            <line key={y} x1="0" x2="100%" y1={y} y2={y} />
          ))}
        </g>
        <g stroke="var(--on-panel)" strokeOpacity="0.16" strokeWidth="1.5">
          <circle cx="100%" cy="100%" r="180" />
          <circle cx="100%" cy="100%" r="290" />
        </g>
        <circle cx="100%" cy="100%" r="90" fill="var(--accent)" />
      </svg>
      <div className="site__browser">
        <div className="site__browser-bar">
          <span />
          <span />
          <span />
        </div>
        <div className="site__browser-body">
          <div className="site__mini-nav">
            <div className="ui-serif site__mini-logo">{"Ledgr"}</div>
            <div className="site__mini-on">{"Home"}</div>
            <div>{"Capture"}</div>
            <div>{"Review"}</div>
            <div>{"Invoices"}</div>
            <div>{"Grootboek"}</div>
          </div>
          <div className="site__mini-main">
            <div className="ui-serif site__mini-title">{"Good afternoon, Max"}</div>
            <div className="site__mini-kpis">
              {(
                [
                  ["Cash position", SAMPLE.cash],
                  ["Receivables", SAMPLE.receivables],
                  ["BTW estimate", SAMPLE.btw],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="ui-card site__mini-kpi">
                  <div>{label}</div>
                  <div className="ui-mono">{value}</div>
                </div>
              ))}
            </div>
            <div className="site__mini-cols">
              <div className="ui-card site__mini-card site__mini-card--wide">
                <b>{"Cash position"}</b>
                <svg width="100%" viewBox="0 0 230 110">
                  <path
                    d="M8 82 L52 66 L96 74 L140 48 L184 38 L222 20 L222 100 L8 100 Z"
                    fill="var(--brand)"
                    fillOpacity="0.16"
                  />
                  <path
                    d="M8 82 L52 66 L96 74 L140 48 L184 38 L222 20"
                    fill="none"
                    stroke="var(--brand)"
                    strokeWidth="2.5"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
              <div className="ui-card site__mini-card">
                <b>{"Needs attention"}</b>
                <div>
                  <span className="ui-badge ui-badge--draft">{"Draft"}</span>
                  <div>{"Invoice 2026-044"}</div>
                </div>
                <div>
                  <span className="ui-badge ui-badge--overdue">{"Overdue"}</span>
                  <div>{"Invoice 2026-032"}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="site__float site__float--receipt">
        <span className="site__float-check">
          <Check size={20} strokeWidth={2.4} />
        </span>
        <div>
          <div className="site__float-title">{"Receipt booked"}</div>
          <div className="site__float-sub">
            {"Papierhuis Amsterdam · "}
            <span className="ui-mono">€ 52,80</span>
          </div>
        </div>
      </div>
      <div className="site__float site__float--btw">
        <div className="site__float-sub">{"Next BTW return"}</div>
        <div className="ui-serif site__float-q">Q3 2026</div>
        <div className="site__float-sub">{"Due in 42 days"}</div>
      </div>
    </div>
  );
}

function PhoneMock() {
  return (
    <div className="site__phone-wrap" aria-hidden="true">
      <div className="site__phone">
        <div className="site__screen">
          <div className="site__notch" />
          <div className="site__scan">
            <i />
            <i />
            <i />
            <i />
          </div>
          <div className="site__receipt">
            <b>{"Papierhuis Amsterdam"}</b>
            <div>{"18 Sep 2026"}</div>
            <hr />
            <div className="site__split">
              <span>{"Printer paper"}</span>
              <span>43,64</span>
            </div>
            <div className="site__split">
              <span>{"BTW 21%"}</span>
              <span>9,16</span>
            </div>
            <hr />
            <div className="site__split site__split--b">
              <span>{"Total"}</span>
              <span>52,80</span>
            </div>
          </div>
          <div className="site__sheet">
            <div className="site__split">
              <b>{"Papierhuis Amsterdam"}</b>
              <span className="ui-mono">€ 52,80</span>
            </div>
            <div className="site__sheet-sub">{"4300 Kantoorkosten · BTW 21%"}</div>
            <div className="site__sheet-btn">{"Book entry"}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

function JournalMock() {
  const rows: ReadonlyArray<readonly [string, string, string]> = [
    ["4300 Kantoorkosten", "43,64", ""],
    ["1520 BTW te vorderen", "9,16", ""],
    ["1100 Bank", "", "52,80"],
  ];
  return (
    <div className="ui-card site__journal" aria-hidden="true">
      <div className="site__journal-head">
        <b>{"Journal entry 2026-0918"}</b>
        <span className="ui-badge ui-badge--booked">{"Booked"}</span>
      </div>
      <div className="site__jrow site__jrow--head">
        <span>{"Account"}</span>
        <span>{"Debit"}</span>
        <span>{"Credit"}</span>
      </div>
      {rows.map(([account, debit, credit]) => (
        <div key={account} className="site__jrow">
          <span>{account}</span>
          <span className="ui-mono">{debit}</span>
          <span className="ui-mono">{credit}</span>
        </div>
      ))}
      <div className="site__jrow site__jrow--total">
        <span>{"Total"}</span>
        <span className="ui-mono">52,80</span>
        <span className="ui-mono">52,80</span>
      </div>
      <div className="site__balanced">
        <Check size={20} strokeWidth={2.4} />
        <span>{COPY.books.balanced}</span>
      </div>
    </div>
  );
}

function ClientsMock() {
  const clients: ReadonlyArray<readonly [string, string, string, AvatarTone]> = [
    ["Datapal BV", "Currently open", "DB", "brand"],
    ["Studio Noord", "2 items need attention", "SN", "accent"],
    ["De Molen Bakkerij", "BTW Q3 ready to review", "DM", "info"],
    ["Van Beek Advies", "All up to date", "VB", "ink"],
  ];
  return (
    <div className="ui-card site__clients" aria-hidden="true">
      <div className="site__clients-title">{COPY.accountants.heading}</div>
      {clients.map(([name, note, initials, tone], index) => (
        <div key={name} className={`site__client${index === 0 ? " site__client--on" : ""}`}>
          <ClientAvatar name={initials} tone={tone} size={44} />
          <div className="site__client-text">
            <b>{name}</b>
            <span>{note}</span>
          </div>
          {index === 0 ? <span className="ui-badge ui-badge--booked">{"Active"}</span> : null}
        </div>
      ))}
    </div>
  );
}
