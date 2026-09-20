import type { HTMLAttributes, ReactNode } from "react";

/** Raised surface, 1px hairline, radius 12, no shadow, no nesting (section 5, Card). */
export function Card({
  padded,
  className,
  ...rest
}: HTMLAttributes<HTMLElement> & { padded?: boolean }) {
  return (
    <section
      className={`ui-card${padded ? " ui-card--pad" : ""}${className ? ` ${className}` : ""}`}
      {...rest}
    />
  );
}

/** A card's title row: heading on the left, optional badge or note after it. */
export function CardHead({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="ui-card__head">
      <h2 className="ui-card__title">{title}</h2>
      {children}
    </div>
  );
}

/**
 * A list row inside a card: 1px top rule, 0 20px padding. Minimum height is the
 * caller's (72 for the attention list, 51 for activity).
 */
export function Row({
  height,
  className,
  style,
  ...rest
}: HTMLAttributes<HTMLElement> & { height: number }) {
  return (
    <div
      className={`ui-row${className ? ` ${className}` : ""}`}
      style={{ minHeight: height, ...style }}
      {...rest}
    />
  );
}

/**
 * The KPI card (section 5): label, `figure-lg` value, then a caption line. The
 * gold delta chip is optional and is omitted when there is no data for it.
 */
export function KpiCard({
  label,
  value,
  chip,
  caption,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  chip?: ReactNode;
  caption?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="ui-card ui-kpi" data-testid={testId}>
      <p className="ui-kpi__label">{label}</p>
      <p className="ui-kpi__value">{value}</p>
      {chip || caption ? (
        <p className="ui-kpi__caption">
          {chip ? <span className="ui-badge ui-badge--delta">{chip}</span> : null}
          {caption}
        </p>
      ) : null}
    </div>
  );
}
