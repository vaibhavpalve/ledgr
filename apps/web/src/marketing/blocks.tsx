import { useEffect, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  Check,
  Eye,
  Globe,
  History,
  KeyRound,
  Landmark,
  Lock,
  Percent,
  ReceiptText,
  Shield,
  Users,
  type LucideIcon,
} from "lucide-react";

import { COMMON } from "./content/common";
import { PLANS, PRICING } from "./content/pricing";
import { Media, type MediaKind } from "./Mockups";
import { useL, type L } from "./l10n";

/** "{year}" style placeholders, filled in. */
export function fill(text: string, values: Record<string, string | number>): string {
  return text.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in values ? String(values[name]) : match,
  );
}

const ICONS: Record<string, LucideIcon> = {
  receipt: ReceiptText,
  bank: Landmark,
  percent: Percent,
  users: Users,
  eye: Eye,
  history: History,
  shield: Shield,
  globe: Globe,
  lock: Lock,
  key: KeyRound,
};

export function CardIcon({ name }: { name: string }) {
  const Glyph = ICONS[name] ?? Check;
  return (
    <span className="mk-ic" aria-hidden="true">
      <Glyph size={22} strokeWidth={1.75} />
    </span>
  );
}

export function Arrow() {
  return <ArrowRight size={16} strokeWidth={2} aria-hidden="true" />;
}

const SITE_URL = "https://boeklite.nl";

/**
 * Per-page title, description, canonical URL and Open Graph tags (the brief). The site is a
 * client-rendered part of the app, so these are set on mount and the title restored on the way out.
 */
export function useSeo(title: L, description: L, path: string) {
  const l = useL();
  const pageTitle = l(title);
  const pageDescription = l(description);
  useEffect(() => {
    const previous = document.title;
    document.title = pageTitle;
    const set = (selector: string, create: () => HTMLElement, attr: string, value: string) => {
      let element = document.head.querySelector<HTMLElement>(selector);
      if (!element) {
        element = create();
        document.head.appendChild(element);
      }
      element.setAttribute(attr, value);
    };
    const meta = (key: "name" | "property", name: string) => () => {
      const element = document.createElement("meta");
      element.setAttribute(key, name);
      return element;
    };
    set('meta[name="description"]', meta("name", "description"), "content", pageDescription);
    set('meta[property="og:title"]', meta("property", "og:title"), "content", pageTitle);
    set(
      'meta[property="og:description"]',
      meta("property", "og:description"),
      "content",
      pageDescription,
    );
    set('meta[property="og:type"]', meta("property", "og:type"), "content", "website");
    set('meta[property="og:url"]', meta("property", "og:url"), "content", SITE_URL + path);
    set(
      'link[rel="canonical"]',
      () => {
        const link = document.createElement("link");
        link.rel = "canonical";
        return link;
      },
      "href",
      SITE_URL + path,
    );
    return () => {
      document.title = previous;
    };
  }, [pageTitle, pageDescription, path]);
}

/** The forest band at the top of every page but the home page. */
export function PageHero({
  eyebrow,
  lead,
  mark,
  body,
  children,
}: {
  eyebrow: L;
  lead: L;
  mark?: L;
  body?: L;
  children?: ReactNode;
}) {
  const l = useL();
  return (
    <section className="mk-hero mk-hero--page">
      <div className="mk-wrap">
        <div className="mk-hero-copy">
          <div className="mk-eyebrow">{l(eyebrow)}</div>
          <h1 className="mk-display">
            {l(lead)}
            {mark ? (
              <>
                {" "}
                <span className="mk-hl">{l(mark)}</span>
              </>
            ) : null}
          </h1>
          {body ? <p className="mk-lead">{l(body)}</p> : null}
          {children}
        </div>
      </div>
    </section>
  );
}

export function StartButtons({ secondary }: { secondary?: { label: L; to: string } }) {
  const l = useL();
  return (
    <div className="mk-cta-row">
      <Link className="mk-btn mk-btn--lemon" to="/signup">
        {l(COMMON.startFree30)}
      </Link>
      <Link className="mk-btn mk-btn--ghost-light" to={secondary?.to ?? "/demo"}>
        {l(secondary?.label ?? COMMON.bookDemo)}
      </Link>
    </div>
  );
}

export function SectionHead({
  eyebrow,
  title,
  body,
  center = false,
  as: Heading = "h2",
}: {
  eyebrow?: L;
  title: L;
  body?: L;
  center?: boolean;
  as?: "h1" | "h2";
}) {
  const l = useL();
  return (
    <div className={`mk-section-head${center ? " mk-center" : ""}`}>
      {eyebrow ? <div className="mk-eyebrow">{l(eyebrow)}</div> : null}
      <Heading className="mk-h2">{l(title)}</Heading>
      {body ? <p className="mk-lead">{l(body)}</p> : null}
    </div>
  );
}

export function Ticks({ items }: { items: readonly L[] }) {
  const l = useL();
  return (
    <ul className="mk-ticks">
      {items.map((item) => (
        <li key={item.en}>
          <Check size={18} strokeWidth={2} aria-hidden="true" />
          <span>{l(item)}</span>
        </li>
      ))}
    </ul>
  );
}

/** One feature: copy on one side, a product mockup on the other. */
export function FeatureRow({
  id,
  eyebrow,
  title,
  body,
  ticks,
  media,
  flip = false,
  link,
}: {
  id?: string;
  eyebrow: L;
  title: L;
  body: L;
  ticks: readonly L[];
  media: MediaKind;
  flip?: boolean;
  link?: { label: L; to: string };
}) {
  const l = useL();
  return (
    <div className={`mk-feature${flip ? " mk-feature--flip" : ""}`} id={id}>
      <div>
        <div className="mk-eyebrow">{l(eyebrow)}</div>
        <h2 className="mk-h2">{l(title)}</h2>
        <p className="mk-lead">{l(body)}</p>
        <Ticks items={ticks} />
        {link ? (
          <Link className="mk-link" to={link.to}>
            {l(link.label)}
            <Arrow />
          </Link>
        ) : null}
      </div>
      <div className="mk-feature-media">
        <Media kind={media} />
      </div>
    </div>
  );
}

export function Faq({ title, items }: { title: L; items: readonly { q: L; a: L }[] }) {
  const l = useL();
  return (
    <section className="mk-section">
      <div className="mk-wrap">
        <SectionHead title={title} center />
        <div className="mk-faq">
          {items.map((item) => (
            <details key={item.q.en}>
              <summary>{l(item.q)}</summary>
              <p>{l(item.a)}</p>
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}

export function CtaBand({
  title = COMMON.cta.title,
  body = COMMON.cta.body,
}: {
  title?: L;
  body?: L;
}) {
  const l = useL();
  return (
    <section className="mk-section--tight">
      <div className="mk-wrap">
        <div className="mk-cta-band">
          <div>
            <h2 className="mk-h2">{l(title)}</h2>
            <p>{l(body)}</p>
          </div>
          <div className="mk-cta-row">
            <Link className="mk-btn mk-btn--lemon" to="/signup">
              {l(COMMON.startFree)}
            </Link>
            <Link className="mk-btn mk-btn--ghost-light" to="/demo">
              {l(COMMON.bookDemo)}
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

/** The three plans; prices switch with `yearly`. Shared by the home page and `/pricing`. */
export function PlanCards({ yearly }: { yearly: boolean }) {
  const l = useL();
  return (
    <div className="mk-plans">
      {PLANS.map((plan) => {
        const price = yearly ? plan.yearly : plan.monthly;
        return (
          <div
            key={plan.id}
            id={plan.id}
            className={`mk-plan${plan.featured ? " mk-plan--featured" : ""}`}
            data-testid={`plan-${plan.id}`}
          >
            {plan.featured ? <span className="mk-badge">{l(PRICING.mostChosen)}</span> : null}
            <h3>{l(plan.name)}</h3>
            <p className="mk-for">{l(plan.for)}</p>
            <div className="mk-price">
              {price !== null ? (
                <>
                  <b data-testid={`price-${plan.id}`}>{`€${price}`}</b>
                  <span>{l(PRICING.billing.perMonth)}</span>
                </>
              ) : (
                <b className="mk-price--words">{l(PRICING.billing.onRequest)}</b>
              )}
            </div>
            <p className="mk-note">
              {price === null
                ? l(PRICING.billing.perClient)
                : yearly
                  ? fill(l(PRICING.billing.billedYearly), { amount: price * 12 })
                  : null}
            </p>
            <Link
              className={`mk-btn ${plan.featured ? "mk-btn--primary" : "mk-btn--secondary"}`}
              to={price === null ? "/demo" : "/signup"}
            >
              {l(price === null ? COMMON.bookDemo : COMMON.startFree)}
            </Link>
            <Ticks items={plan.ticks} />
          </div>
        );
      })}
    </div>
  );
}
