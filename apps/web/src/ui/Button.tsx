import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link, type LinkProps } from "react-router-dom";

export type ButtonVariant = "primary" | "secondary" | "ghost";
export type ButtonSize = "sm" | "md" | "lg" | "xl";

interface Look {
  variant?: ButtonVariant;
  size?: ButtonSize;
  block?: boolean;
  /** A square, icon-only button; the caller must supply `aria-label`. */
  iconOnly?: boolean;
}

function classes({ variant = "secondary", size = "md", block, iconOnly }: Look, extra?: string) {
  return [
    "ui-btn",
    variant !== "secondary" ? `ui-btn--${variant}` : "",
    size !== "md" ? `ui-btn--${size}` : "",
    block ? "ui-btn--block" : "",
    iconOnly ? "ui-btn--icon" : "",
    extra ?? "",
  ]
    .filter(Boolean)
    .join(" ");
}

/** DESIGN.md section 5, Button. One primary per view. */
interface ButtonProps extends Look, ButtonHTMLAttributes<HTMLButtonElement> {
  children?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant, size, block, iconOnly, className, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={classes({ variant, size, block, iconOnly }, className)}
      {...rest}
    />
  );
});

/** The same look on a router link, for navigation that is a link and not an action. */
export function ButtonLink({
  variant,
  size,
  block,
  iconOnly,
  className,
  ...rest
}: LinkProps & Look) {
  return <Link className={classes({ variant, size, block, iconOnly }, className)} {...rest} />;
}
