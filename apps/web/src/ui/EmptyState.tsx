import type { ReactNode } from "react";

/** A dashed, calm message for a region with nothing in it yet. Says what to do next. */
export function EmptyState({
  title,
  children,
  action,
  icon,
  testId,
}: {
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="ui-empty" data-testid={testId}>
      {icon}
      <p className="ui-empty__title">{title}</p>
      {children ? <p className="ui-empty__body">{children}</p> : null}
      {action}
    </div>
  );
}
