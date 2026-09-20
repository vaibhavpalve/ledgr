import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { LucideIcon } from "lucide-react";

/**
 * A sidebar item: height 40, icon 20 at stroke 1.7. Current page is marked
 * with `aria-current` (NavLink) and drawn as the gold pill, the one highlight
 * per view (section 5, Nav item).
 */
export function NavItem({
  to,
  end,
  icon: Icon,
  children,
  badge,
  onNavigate,
  testId,
}: {
  to: string;
  end?: boolean;
  icon: LucideIcon;
  children: ReactNode;
  badge?: ReactNode;
  onNavigate?: () => void;
  testId?: string;
}) {
  return (
    <NavLink to={to} end={end} className="ui-nav" onClick={onNavigate} data-testid={testId}>
      <Icon size={20} strokeWidth={1.7} aria-hidden="true" />
      <span className="ui-nav__label">{children}</span>
      {badge}
    </NavLink>
  );
}

export function NavGroup({ children }: { children: ReactNode }) {
  return <div className="ui-navgroup">{children}</div>;
}
