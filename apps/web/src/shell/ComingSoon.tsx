import { useI18n } from "@ledgr/i18n";
import { Building, Landmark, ScrollText, ChartColumn, type LucideIcon } from "lucide-react";

import { EmptyState, PageHeader } from "./ScreenState";

/**
 * The four rail items whose screens do not exist yet, each with the label and
 * glyph the rail gives it. One list, so a screen that is later built is removed
 * here and from `App.tsx`'s routes and nothing else.
 */
export const COMING_SOON = {
  bank: { labelKey: "common.nav.bank", icon: Landmark },
  journal: { labelKey: "common.nav.journal", icon: ScrollText },
  assets: { labelKey: "common.nav.assets", icon: Building },
  reports: { labelKey: "common.nav.reports", icon: ChartColumn },
} as const satisfies Record<string, { labelKey: string; icon: LucideIcon }>;

export type ComingSoonSection = keyof typeof COMING_SOON;

/**
 * A rail item that is in the menu before its screen is. It says so, in the
 * item's own name, instead of opening an empty table that looks like a bug.
 */
export function ComingSoon({ section }: { section: ComingSoonSection }) {
  const { t } = useI18n();
  const { labelKey, icon: Glyph } = COMING_SOON[section];
  const name = t(labelKey);

  return (
    <section className="screen" aria-label={name} data-testid={`coming-soon-${section}`}>
      <PageHeader title={name} />
      <EmptyState
        icon={<Glyph size={28} strokeWidth={1.5} />}
        title={t("common.soon.title")}
        body={t("common.soon.body", { section: name })}
      />
    </section>
  );
}
