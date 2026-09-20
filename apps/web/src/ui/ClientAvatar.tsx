export type AvatarTone = "brand" | "accent" | "info" | "ink" | "tint";

/** The tones a client can be given, in the order they are handed out. */
export const CLIENT_TONES: readonly AvatarTone[] = ["brand", "accent", "info", "ink"];

/** Up to two initials: first letters of the first two words, else the first two letters. */
export function initialsOf(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  const letters =
    words.length >= 2 ? [words[0]?.[0], words[1]?.[0]] : [words[0]?.[0], words[0]?.[1]];
  return letters.filter(Boolean).join("").toUpperCase();
}

/**
 * Stable tone for an id, so a client keeps the same colour across visits.
 * A string hash, not arithmetic on money.
 */
export function toneFor(id: string): AvatarTone {
  let hash = 0;
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return CLIENT_TONES[hash % CLIENT_TONES.length] ?? "brand";
}

/**
 * Square avatar with initials (section 5, Client avatar). It is decorative:
 * the client's name is always rendered beside it, so it is `aria-hidden`.
 */
export function ClientAvatar({
  name,
  tone = "brand",
  size = 34,
  round,
}: {
  name: string;
  tone?: AvatarTone;
  size?: number;
  round?: boolean;
}) {
  return (
    <span
      className={`ui-avatar${tone !== "brand" ? ` ui-avatar--${tone}` : ""}${round ? " ui-avatar--round" : ""}`}
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      {initialsOf(name)}
    </span>
  );
}
