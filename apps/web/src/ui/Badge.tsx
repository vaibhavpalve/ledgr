import type { HTMLAttributes } from "react";
import { Check } from "lucide-react";

export type BadgeVariant =
  "neutral" | "booked" | "draft" | "overdue" | "review" | "syncing" | "delta";

/**
 * A pill that always carries a word (DESIGN.md principle 4); `booked` adds a
 * check glyph. Colours are the status roles and nothing else.
 */
export function Badge({
  variant = "neutral",
  className,
  children,
  ...rest
}: HTMLAttributes<HTMLSpanElement> & { variant?: BadgeVariant }) {
  return (
    <span
      className={`ui-badge${variant !== "neutral" ? ` ui-badge--${variant}` : ""}${className ? ` ${className}` : ""}`}
      {...rest}
    >
      {variant === "booked" ? <Check size={12} strokeWidth={2.2} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}
