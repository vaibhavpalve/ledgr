import { useId, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { Check, Minus } from "lucide-react";

import { ACCOUNTANTS, SECURITY } from "./content/accountants";
import {
  ARTICLES,
  ARTICLES_PAGE,
  CATEGORIES,
  readingMinutes,
  type Article,
  type Block,
  type Category,
} from "./content/articles";
import { COMMON } from "./content/common";
import { CONTACT_EMAIL, DEMO, LEGAL } from "./content/contact";
import { HOME } from "./content/home";
import { COMPARE, PRICING, type Cell } from "./content/pricing";
import { PRODUCT } from "./content/product";
import {
  Arrow,
  CardIcon,
  CtaBand,
  Faq,
  FeatureRow,
  PageHero,
  PlanCards,
  SectionHead,
  StartButtons,
  Ticks,
  fill,
  useSeo,
} from "./blocks";
import { AppMockup } from "./Mockups";
import { useL, useLang, type L } from "./l10n";

/* ======================================================================= *
 * Home
 * ======================================================================= */

export function HomePage() {
  const l = useL();
  useSeo(HOME.seo.title, HOME.seo.description, "/");
  const [audience, setAudience] = useState<"owners" | "accountants">("owners");
  const tabs = useId();
  const cards = audience === "owners" ? HOME.audience.ownerCards : HOME.audience.accountantCards;
  const f = HOME.features;

  return (
    <>
      <section className="mk-hero mk-hero--home">
        <div className="mk-wrap">
          <div className="mk-hero-copy">
            <div className="mk-eyebrow">{l(HOME.hero.eyebrow)}</div>
            <h1 className="mk-display" data-testid="mk-home-title">
              {l(HOME.hero.lead)} <span className="mk-hl">{l(HOME.hero.mark)}</span>
            </h1>
            <p className="mk-lead">{l(HOME.hero.body)}</p>
            <StartButtons />
            <ul className="mk-micro">
              {HOME.hero.micro.map((item) => (
                <li key={item.en}>
                  <Check size={16} strokeWidth={2} aria-hidden="true" />
                  {l(item)}
                </li>
              ))}
            </ul>
          </div>
          <div className="mk-hero-shot">
            <AppMockup />
          </div>
        </div>
      </section>

      <section className="mk-proof">
        <div className="mk-wrap">
          <ul>
            {HOME.proof.map((item) => (
              <li key={item.title.en}>
                <b>{l(item.title)}</b>
                <span>{l(item.body)}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="mk-section">
        <div className="mk-wrap">
          <SectionHead
            eyebrow={HOME.audience.eyebrow}
            title={HOME.audience.title}
            body={HOME.audience.body}
            center
          />
          <div className="mk-center">
            <div className="mk-switch" role="tablist" aria-label={l(HOME.audience.label)}>
              {(["owners", "accountants"] as const).map((key) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  id={`${tabs}-${key}`}
                  aria-selected={audience === key}
                  aria-controls={`${tabs}-panel`}
                  tabIndex={audience === key ? 0 : -1}
                  onClick={() => setAudience(key)}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
                      const next = key === "owners" ? "accountants" : "owners";
                      setAudience(next);
                      document.getElementById(`${tabs}-${next}`)?.focus();
                    }
                  }}
                  data-testid={`mk-audience-${key}`}
                >
                  {l(key === "owners" ? HOME.audience.owners : HOME.audience.accountants)}
                </button>
              ))}
            </div>
          </div>
          <div id={`${tabs}-panel`} role="tabpanel" aria-labelledby={`${tabs}-${audience}`}>
            <div className="mk-cards-3">
              {cards.map((card) => (
                <div key={card.title.en} className="mk-card">
                  <CardIcon name={card.icon} />
                  <h3 className="mk-h3">{l(card.title)}</h3>
                  <p>{l(card.body)}</p>
                  <Link className="mk-link" to={card.to}>
                    {l(card.link)}
                    <Arrow />
                  </Link>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="mk-section mk-section--sunken">
        <div className="mk-wrap">
          <FeatureRow
            id="capture"
            {...f.capture}
            media="capture"
            link={{ label: f.capture.link, to: "/product#capture" }}
          />
          <FeatureRow
            id="bank"
            {...f.bank}
            media="bank"
            flip
            link={{ label: f.bank.link, to: "/product#bank" }}
          />
          <FeatureRow
            id="btw"
            {...f.btw}
            media="btw"
            link={{ label: f.btw.link, to: "/product#btw" }}
          />
        </div>
      </section>

      <section className="mk-section mk-section--forest">
        <div className="mk-wrap">
          <SectionHead eyebrow={HOME.steps.eyebrow} title={HOME.steps.title} />
          <ol className="mk-steps">
            {HOME.steps.items.map((step) => (
              <li key={step.title.en} className="mk-step">
                <h3 className="mk-h3">{l(step.title)}</h3>
                <p>{l(step.body)}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="mk-section">
        <div className="mk-wrap mk-center">
          <SectionHead
            eyebrow={HOME.integrations.eyebrow}
            title={HOME.integrations.title}
            body={HOME.integrations.body}
            center
          />
          <ul className="mk-logos">
            {HOME.integrations.now.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
          <div className="mk-soon">
            <span>{l(HOME.integrations.soonLabel)}</span>
            {HOME.integrations.soon.map((item) => (
              <span key={item.en} className="mk-chip mk-chip--soon">
                {l(item)}
              </span>
            ))}
          </div>
        </div>
      </section>

      <section className="mk-section mk-section--sunken">
        <div className="mk-wrap">
          <SectionHead
            eyebrow={HOME.pricing.eyebrow}
            title={HOME.pricing.title}
            body={HOME.pricing.body}
            center
          />
          <PlanCards yearly={false} />
          <p className="mk-center" style={{ marginTop: 28 }}>
            <Link className="mk-link" to="/pricing">
              {l(HOME.pricing.compare)}
              <Arrow />
            </Link>
          </p>
        </div>
      </section>

      <LatestArticles />
      <Faq title={HOME.faqTitle} items={HOME.faq} />
      <CtaBand />
    </>
  );
}

function LatestArticles() {
  const l = useL();
  const language = useLang();
  return (
    <section className="mk-section">
      <div className="mk-wrap">
        <SectionHead
          eyebrow={ARTICLES_PAGE.hero.eyebrow}
          title={{ en: "Bookkeeping, explained.", nl: "Boekhouden, uitgelegd." }}
          body={ARTICLES_PAGE.hero.body}
          center
        />
        <div className="mk-cards-3">
          {ARTICLES.slice(0, 3).map((article) => (
            <ArticleCard key={article.slug} article={article} language={language} l={l} />
          ))}
        </div>
        <p className="mk-center" style={{ marginTop: 28 }}>
          <Link className="mk-link" to="/articles">
            {l(COMMON.footer.allArticles)}
            <Arrow />
          </Link>
        </p>
      </div>
    </section>
  );
}

/* ======================================================================= *
 * Product, accountants, security
 * ======================================================================= */

export function ProductPage() {
  const l = useL();
  useSeo(PRODUCT.seo.title, PRODUCT.seo.description, "/product");
  return (
    <>
      <PageHero {...PRODUCT.hero}>
        <StartButtons />
      </PageHero>
      <section className="mk-section--tight">
        <div className="mk-wrap">
          <div className="mk-cards-3">
            {PRODUCT.sections.slice(0, 6).map((section) => (
              <a key={section.id} className="mk-card" href={`#${section.id}`}>
                <h2 className="mk-h3">{l(section.eyebrow)}</h2>
                <p>{l(section.title)}</p>
              </a>
            ))}
          </div>
        </div>
      </section>
      <section className="mk-section">
        <div className="mk-wrap">
          {PRODUCT.sections.map((section, index) => (
            <FeatureRow
              key={section.id}
              id={section.id}
              eyebrow={section.eyebrow}
              title={section.title}
              body={section.body}
              ticks={section.ticks}
              media={section.media}
              flip={index % 2 === 1}
            />
          ))}
        </div>
      </section>
      <CtaBand title={PRODUCT.cta.title} body={PRODUCT.cta.body} />
    </>
  );
}

export function AccountantsPage() {
  const l = useL();
  useSeo(ACCOUNTANTS.seo.title, ACCOUNTANTS.seo.description, "/accountants");
  return (
    <>
      <PageHero
        eyebrow={ACCOUNTANTS.hero.eyebrow}
        lead={ACCOUNTANTS.hero.lead}
        mark={ACCOUNTANTS.hero.mark}
        body={ACCOUNTANTS.hero.body}
      >
        <div className="mk-cta-row">
          <Link className="mk-btn mk-btn--lemon" to="/demo">
            {l(COMMON.bookDemo)}
          </Link>
          <Link className="mk-btn mk-btn--ghost-light" to="/pricing#accountant">
            {l(ACCOUNTANTS.hero.secondary)}
          </Link>
        </div>
      </PageHero>
      <section className="mk-section">
        <div className="mk-wrap">
          <div className="mk-cards-4" style={{ marginBottom: 96 }}>
            {ACCOUNTANTS.pillars.map((pillar) => (
              <div key={pillar.title.en} className="mk-card">
                <CardIcon name={pillar.icon} />
                <h2 className="mk-h3">{l(pillar.title)}</h2>
                <p>{l(pillar.body)}</p>
              </div>
            ))}
          </div>
          {ACCOUNTANTS.sections.map((section, index) => (
            <FeatureRow
              key={section.id}
              id={section.id}
              eyebrow={section.eyebrow}
              title={section.title}
              body={section.body}
              ticks={section.ticks}
              media={section.media}
              flip={index % 2 === 1}
            />
          ))}
        </div>
      </section>
      <Faq title={ACCOUNTANTS.faqTitle} items={ACCOUNTANTS.faq} />
      <CtaBand title={ACCOUNTANTS.cta.title} body={ACCOUNTANTS.cta.body} />
    </>
  );
}

export function SecurityPage() {
  const l = useL();
  useSeo(SECURITY.seo.title, SECURITY.seo.description, "/security");
  return (
    <>
      <PageHero {...SECURITY.hero} />
      <section className="mk-section">
        <div className="mk-wrap">
          <div className="mk-cards-3" style={{ marginBottom: 120 }}>
            {SECURITY.pillars.map((pillar) => (
              <div key={pillar.title.en} className="mk-card">
                <CardIcon name={pillar.icon} />
                <h2 className="mk-h3">{l(pillar.title)}</h2>
                <p>{l(pillar.body)}</p>
              </div>
            ))}
          </div>
          <FeatureRow
            id="audit"
            eyebrow={SECURITY.audit.eyebrow}
            title={SECURITY.audit.title}
            body={SECURITY.audit.body}
            ticks={SECURITY.audit.ticks}
            media="grootboek"
          />
        </div>
      </section>
      <Faq title={SECURITY.faqTitle} items={SECURITY.faq} />
      <CtaBand title={SECURITY.cta.title} body={SECURITY.cta.body} />
    </>
  );
}

/* ======================================================================= *
 * Pricing
 * ======================================================================= */

function CellView({ cell }: { cell: Cell }) {
  const l = useL();
  if (cell === "yes") {
    return (
      <>
        <Check size={18} strokeWidth={2} aria-hidden="true" />
        <span className="mk-visually-hidden">{l(PRICING.included)}</span>
      </>
    );
  }
  if (cell === "no") {
    return (
      <span className="mk-no">
        <Minus size={18} strokeWidth={2} aria-hidden="true" style={{ display: "inline-block" }} />
        <span className="mk-visually-hidden">{l(PRICING.notIncluded)}</span>
      </span>
    );
  }
  if (cell === "soon") return <span className="mk-chip mk-chip--soon">{l(PRICING.soon)}</span>;
  return <>{l(cell)}</>;
}

export function PricingPage() {
  const l = useL();
  useSeo(PRICING.seo.title, PRICING.seo.description, "/pricing");
  const [yearly, setYearly] = useState(false);
  return (
    <>
      <PageHero {...PRICING.hero} />
      <section className="mk-section">
        <div className="mk-wrap">
          <div className="mk-center">
            <div className="mk-toggle">
              <span aria-hidden="true">{l(PRICING.billing.monthly)}</span>
              <button
                type="button"
                role="switch"
                aria-checked={yearly}
                aria-label={l(PRICING.billing.label)}
                onClick={() => setYearly((value) => !value)}
                data-testid="mk-billing"
              />
              <span aria-hidden="true">{l(PRICING.billing.yearly)}</span>
              <span className="mk-save">{l(PRICING.billing.save)}</span>
            </div>
          </div>
          <PlanCards yearly={yearly} />
        </div>
      </section>
      <section className="mk-section mk-section--sunken">
        <div className="mk-wrap">
          <SectionHead title={PRICING.compareTitle} center />
          <div className="mk-compare-wrap">
            <table className="mk-compare">
              <thead>
                <tr>
                  <th scope="col">{l(PRICING.feature)}</th>
                  <th scope="col">{l({ en: "Start", nl: "Start" })}</th>
                  <th scope="col">{l({ en: "Grow", nl: "Groei" })}</th>
                  <th scope="col">{l({ en: "Accountant", nl: "Accountant" })}</th>
                </tr>
              </thead>
              <tbody>
                {COMPARE.flatMap((group) => [
                  <tr key={group.group.en} className="mk-grp">
                    <th colSpan={4} scope="colgroup">
                      {l(group.group)}
                    </th>
                  </tr>,
                  ...group.rows.map((row) => (
                    <tr key={row.label.en}>
                      <th scope="row">{l(row.label)}</th>
                      {row.cells.map((cell, index) => (
                        <td key={index}>
                          <CellView cell={cell} />
                        </td>
                      ))}
                    </tr>
                  )),
                ])}
              </tbody>
            </table>
          </div>
        </div>
      </section>
      <Faq title={PRICING.faqTitle} items={PRICING.faq} />
      <CtaBand />
    </>
  );
}

/* ======================================================================= *
 * Demo / contact
 * ======================================================================= */

/**
 * The demo request. There is no form backend yet, so submitting opens the visitor's own mail app
 * with the request filled in, addressed to us, and says so; nothing is sent behind their back.
 */
export function DemoPage() {
  const l = useL();
  useSeo(DEMO.seo.title, DEMO.seo.description, "/demo");
  const form = DEMO.form;
  const id = useId();
  const [values, setValues] = useState({
    name: "",
    company: "",
    email: "",
    phone: "",
    role: 0,
    message: "",
  });
  const [problem, setProblem] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const set = (key: keyof typeof values) => (value: string | number) =>
    setValues((current) => ({ ...current, [key]: value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!values.name.trim() || !values.company.trim() || !values.email.trim()) {
      setProblem(l(form.required));
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email.trim())) {
      setProblem(l(form.invalidEmail));
      return;
    }
    setProblem(null);
    const role = form.roles[values.role] ?? form.roles[0];
    const body = [
      `${l(form.name)}: ${values.name}`,
      `${l(form.company)}: ${values.company}`,
      `${l(form.email)}: ${values.email}`,
      values.phone ? `${l(form.phone)}: ${values.phone}` : null,
      `${l(form.role)}: ${role ? l(role) : ""}`,
      "",
      values.message,
    ]
      .filter((line) => line !== null)
      .join("\n");
    const subject = fill(l(form.subject), { company: values.company });
    window.location.href = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    setSent(true);
  };

  const field = (
    key: "name" | "company" | "email" | "phone",
    label: L,
    type = "text",
    auto?: string,
  ) => (
    <div className="mk-field">
      <label htmlFor={`${id}-${key}`}>{l(label)}</label>
      <input
        id={`${id}-${key}`}
        className="mk-input"
        type={type}
        autoComplete={auto}
        required={key !== "phone"}
        value={values[key]}
        onChange={(event) => set(key)(event.target.value)}
        data-testid={`mk-demo-${key}`}
      />
    </div>
  );

  return (
    <>
      <PageHero {...DEMO.hero} />
      <section className="mk-section">
        <div className="mk-wrap mk-two-col">
          <form className="mk-form" onSubmit={submit} noValidate data-testid="mk-demo-form">
            <h2 className="mk-h3">{l(form.title)}</h2>
            <div className="mk-form-grid">
              {field("name", form.name, "text", "name")}
              {field("company", form.company, "text", "organization")}
              {field("email", form.email, "email", "email")}
              {field("phone", form.phone, "tel", "tel")}
              <div className="mk-field mk-full">
                <label htmlFor={`${id}-role`}>{l(form.role)}</label>
                <select
                  id={`${id}-role`}
                  className="mk-input"
                  value={values.role}
                  onChange={(event) => set("role")(Number(event.target.value))}
                >
                  {form.roles.map((role, index) => (
                    <option key={role.en} value={index}>
                      {l(role)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="mk-field mk-full">
                <label htmlFor={`${id}-message`}>{l(form.message)}</label>
                <textarea
                  id={`${id}-message`}
                  className="mk-input"
                  value={values.message}
                  onChange={(event) => set("message")(event.target.value)}
                />
              </div>
            </div>
            {problem !== null ? (
              <p className="mk-error" role="alert" data-testid="mk-demo-error">
                {problem}
              </p>
            ) : null}
            <button type="submit" className="mk-btn mk-btn--primary" data-testid="mk-demo-submit">
              {l(form.submit)}
            </button>
            <p className="mk-form-note">{l(form.privacy)}</p>
            {sent ? (
              <div className="mk-success" role="status" data-testid="mk-demo-sent">
                <b>{l(form.sentTitle)}</b>
                {fill(l(form.sentBody), { email: CONTACT_EMAIL })}
              </div>
            ) : null}
          </form>
          <div className="mk-aside">
            <div className="mk-card">
              <h2 className="mk-h3">{l(DEMO.side.title)}</h2>
              <Ticks items={DEMO.side.ticks} />
            </div>
            <div className="mk-card">
              <h3 className="mk-h3">{l(DEMO.side.tryTitle)}</h3>
              <p>{l(DEMO.side.tryBody)}</p>
              <Link className="mk-link" to="/signup">
                {l(COMMON.startFree)}
                <Arrow />
              </Link>
            </div>
            <div className="mk-card">
              <h3 className="mk-h3">{l(DEMO.side.otherTitle)}</h3>
              <p>
                <a className="mk-link" href={`mailto:${CONTACT_EMAIL}`}>
                  {CONTACT_EMAIL}
                </a>
              </p>
            </div>
          </div>
        </div>
      </section>
    </>
  );
}

/* ======================================================================= *
 * Articles
 * ======================================================================= */

function formatDate(iso: string, language: "en" | "nl"): string {
  const [year, month, day] = iso.split("-").map(Number);
  return new Intl.DateTimeFormat(language === "nl" ? "nl-NL" : "en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(year ?? 2026, (month ?? 1) - 1, day ?? 1)));
}

function ArticleMeta({
  article,
  language,
  l,
}: {
  article: Article;
  language: "en" | "nl";
  l: (text: L) => string;
}) {
  return (
    <div className="mk-article-meta">
      <span className="mk-chip">{l(CATEGORIES[article.category])}</span>
      <time dateTime={article.published}>{formatDate(article.published, language)}</time>
      <span>{fill(l(ARTICLES_PAGE.minutes), { count: readingMinutes(article, language) })}</span>
    </div>
  );
}

function ArticleCard({
  article,
  language,
  l,
  lead = false,
}: {
  article: Article;
  language: "en" | "nl";
  l: (text: L) => string;
  lead?: boolean;
}) {
  return (
    <Link
      to={`/articles/${article.slug}`}
      className={`mk-card mk-article-card${lead ? " mk-article-card--lead" : ""}`}
      data-testid={`mk-article-${article.slug}`}
    >
      <ArticleMeta article={article} language={language} l={l} />
      <h3 className="mk-h3">{l(article.title)}</h3>
      <p>{l(article.summary)}</p>
      <span className="mk-link">
        {l(COMMON.readMore)}
        <Arrow />
      </span>
    </Link>
  );
}

export function ArticlesPage() {
  const l = useL();
  const language = useLang();
  useSeo(ARTICLES_PAGE.seo.title, ARTICLES_PAGE.seo.description, "/articles");
  const [category, setCategory] = useState<Category | null>(null);
  const shown = ARTICLES.filter((article) => category === null || article.category === category);
  const categories = Object.keys(CATEGORIES) as Category[];
  return (
    <>
      <PageHero {...ARTICLES_PAGE.hero} />
      <section className="mk-section">
        <div className="mk-wrap">
          <div className="mk-filters" role="group" aria-label={l(ARTICLES_PAGE.filter)}>
            <button
              type="button"
              aria-pressed={category === null}
              onClick={() => setCategory(null)}
            >
              {l(ARTICLES_PAGE.all)}
            </button>
            {categories.map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={category === key}
                onClick={() => setCategory(key)}
                data-testid={`mk-filter-${key}`}
              >
                {l(CATEGORIES[key])}
              </button>
            ))}
          </div>
          <div className="mk-cards-3" data-testid="mk-article-list">
            {shown.map((article, index) => (
              <ArticleCard
                key={article.slug}
                article={article}
                language={language}
                l={l}
                lead={category === null && index === 0}
              />
            ))}
          </div>
        </div>
      </section>
      <CtaBand title={ARTICLES_PAGE.ctaTitle} body={ARTICLES_PAGE.ctaBody} />
    </>
  );
}

function BlockView({ block, l }: { block: Block; l: (text: L) => string }) {
  switch (block.type) {
    case "p":
      return <p>{l(block.text)}</p>;
    case "h2":
      return <h2>{l(block.text)}</h2>;
    case "ul":
      return (
        <ul>
          {block.items.map((item) => (
            <li key={item.en}>{l(item)}</li>
          ))}
        </ul>
      );
    case "tip":
      return (
        <aside className="mk-tip">
          <b>{l(ARTICLES_PAGE.inBoeklite)}</b>
          {l(block.text)}
        </aside>
      );
    case "table":
      return (
        <div className="mk-prose-table">
          <table>
            <thead>
              <tr>
                {block.head.map((cell) => (
                  <th key={cell.en} scope="col">
                    {l(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, index) => (
                <tr key={index}>
                  {row.map((cell, column) => (
                    <td key={column}>{l(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
  }
}

export function ArticlePage() {
  const { slug } = useParams();
  const article = ARTICLES.find((candidate) => candidate.slug === slug);
  if (!article) return <SiteNotFound />;
  return <ArticleView article={article} />;
}

function ArticleView({ article }: { article: Article }) {
  const l = useL();
  const language = useLang();
  useSeo(
    { en: `${article.title.en} | Boeklite`, nl: `${article.title.nl} | Boeklite` },
    article.summary,
    `/articles/${article.slug}`,
  );
  const related = ARTICLES.filter((other) => other.slug !== article.slug)
    .sort(
      (a, b) => Number(b.category === article.category) - Number(a.category === article.category),
    )
    .slice(0, 3);
  return (
    <>
      <section className="mk-hero mk-hero--page">
        <div className="mk-wrap">
          <div className="mk-article-head">
            <Link className="mk-back" to="/articles">
              <Arrow />
              {l(ARTICLES_PAGE.back)}
            </Link>
            <h1 className="mk-display" data-testid="mk-article-title">
              {l(article.title)}
            </h1>
            <p className="mk-lead">{l(article.summary)}</p>
            <ArticleMeta article={article} language={language} l={l} />
          </div>
        </div>
      </section>
      <section className="mk-section">
        <div className="mk-wrap">
          <article className="mk-prose" data-testid="mk-article-body">
            {article.body.map((block, index) => (
              <BlockView key={index} block={block} l={l} />
            ))}
            <p className="mk-disclaimer">{l(ARTICLES_PAGE.disclaimer)}</p>
          </article>
        </div>
      </section>
      <section className="mk-section mk-section--sunken">
        <div className="mk-wrap">
          <SectionHead title={ARTICLES_PAGE.related} center />
          <div className="mk-cards-3">
            {related.map((other) => (
              <ArticleCard key={other.slug} article={other} language={language} l={l} />
            ))}
          </div>
        </div>
      </section>
      <CtaBand title={ARTICLES_PAGE.ctaTitle} body={ARTICLES_PAGE.ctaBody} />
    </>
  );
}

/* ======================================================================= *
 * Legal and not found
 * ======================================================================= */

export function LegalPage({ kind }: { kind: keyof typeof LEGAL }) {
  const l = useL();
  const page = LEGAL[kind];
  const path = kind === "disclosure" ? "/responsible-disclosure" : `/${kind}`;
  useSeo(page.seo.title, page.seo.description, path);
  return (
    <>
      <PageHero eyebrow={COMMON.footer.company} lead={page.title} body={page.intro} />
      <section className="mk-section">
        <div className="mk-wrap">
          <div className="mk-prose mk-legal" data-testid={`mk-legal-${kind}`}>
            {page.sections.map((section) => (
              <section key={section.title.en}>
                <h2>{l(section.title)}</h2>
                <p>{fill(l(section.body), { email: CONTACT_EMAIL })}</p>
              </section>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}

export function SiteNotFound() {
  const l = useL();
  useSeo(
    { en: "Page not found | Boeklite", nl: "Pagina niet gevonden | Boeklite" },
    COMMON.notFoundBody,
    "/",
  );
  return (
    <PageHero
      eyebrow={{ en: "404", nl: "404" }}
      lead={COMMON.notFoundTitle}
      body={COMMON.notFoundBody}
    >
      <div className="mk-cta-row">
        <Link className="mk-btn mk-btn--lemon" to="/">
          {l(COMMON.backHome)}
        </Link>
        <Link className="mk-btn mk-btn--ghost-light" to="/articles">
          {l(COMMON.footer.allArticles)}
        </Link>
      </div>
    </PageHero>
  );
}
