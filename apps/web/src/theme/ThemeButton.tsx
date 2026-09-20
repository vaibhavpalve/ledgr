import { Moon, Sun } from "lucide-react";
import { useI18n } from "@ledgr/i18n";

import { Button } from "../ui";
import { useTheme } from "./useTheme";

/**
 * The single icon button on the login reference (design/reference/Login.png):
 * it flips the theme you are looking at, light to dark and back, and stores
 * the choice as an explicit `data-theme` (ADR-055's `applyTheme`).
 *
 * "System" is still a real preference; it is a choice you make once in
 * Settings, Appearance (`ThemeToggle`), not one you want to cycle through on
 * every visit. Until you flip this button the page follows the OS.
 */
export function ThemeButton() {
  const { t } = useI18n();
  const { resolved, setPreference } = useTheme();
  const toDark = resolved === "light";
  const Icon = toDark ? Moon : Sun;

  return (
    <Button
      iconOnly
      data-testid="theme-button"
      data-resolved={resolved}
      aria-label={t(toDark ? "auth.theme.switch_to_dark" : "auth.theme.switch_to_light")}
      onClick={() => setPreference(toDark ? "dark" : "light")}
    >
      <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
    </Button>
  );
}
