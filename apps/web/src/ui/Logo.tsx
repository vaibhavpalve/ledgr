/**
 * The placeholder wordmark (DESIGN.md section 3): an outline mark, a rounded
 * square with a vertical rule at a third and two horizontal rules, then the
 * word "Ledgr" in Newsreader 500. Replace when a real logo exists.
 */
export function Logo({ size = 28, onPanel }: { size?: number; onPanel?: boolean }) {
  return (
    <span className="ui-logo" style={onPanel ? { color: "var(--on-panel)" } : undefined}>
      <svg
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
        stroke={onPanel ? "var(--on-panel)" : "currentColor"}
        strokeWidth="2"
        aria-hidden="true"
      >
        <rect x="3" y="3" width="26" height="26" rx="7" />
        <path d="M11 3v26M11 12h18M11 20h18" />
      </svg>
      <span className="ui-logo__word">Ledgr</span>
    </span>
  );
}
