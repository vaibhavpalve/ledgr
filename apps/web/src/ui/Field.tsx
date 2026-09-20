import { useId, type InputHTMLAttributes, type ReactNode } from "react";
import { CircleAlert } from "lucide-react";

/**
 * Label above, control, helper below; an error swaps the helper for a sentence
 * that says what to change and adds a glyph so it never relies on colour alone
 * (DESIGN.md section 5, Input; principle 4).
 */
export function Field({
  label,
  helper,
  error,
  size = "md",
  labelAction,
  id: idProp,
  ...input
}: Omit<InputHTMLAttributes<HTMLInputElement>, "size"> & {
  label: ReactNode;
  helper?: ReactNode;
  error?: ReactNode;
  size?: "md" | "lg";
  /** Sits top-right of the label row, e.g. the password "Show / Hide" button. */
  labelAction?: ReactNode;
}) {
  const generated = useId();
  const id = idProp ?? generated;
  const noteId = `${id}-note`;
  const invalid = error !== undefined && error !== null && error !== false;

  return (
    <div className="ui-field">
      <div className="ui-field__row">
        <label className="ui-label" htmlFor={id}>
          {label}
        </label>
        {labelAction}
      </div>
      <input
        {...input}
        id={id}
        className={`ui-input${size === "lg" ? " ui-input--lg" : ""}`}
        aria-invalid={invalid ? true : undefined}
        aria-describedby={invalid || helper ? noteId : undefined}
      />
      {invalid ? (
        <p id={noteId} className="ui-error" role="alert">
          <CircleAlert size={14} strokeWidth={1.7} aria-hidden="true" />
          <span>{error}</span>
        </p>
      ) : helper ? (
        <p id={noteId} className="ui-help">
          {helper}
        </p>
      ) : null}
    </div>
  );
}
