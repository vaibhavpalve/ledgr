import { useEffect, useRef } from "react";
import { useI18n } from "@ledgr/i18n";
import { EXPENSE_CATEGORIES } from "@ledgr/shared-types";
import type { ExpenseCategoryKey } from "@ledgr/shared-types";
import {
  Briefcase,
  Building,
  Car,
  Cpu,
  FileText,
  GraduationCap,
  House,
  Landmark,
  Megaphone,
  Phone,
  Plane,
  Plus,
  ScanLine,
  Shield,
  ShoppingCart,
  Sparkles,
  Users,
  Utensils,
  UtensilsCrossed,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";

import { useModalFocus } from "../useModalFocus";

/**
 * One glyph per category. Typed as a complete record over the shared list, so a
 * category added there without an icon here is a compile error rather than a
 * tile with a hole in it.
 */
const ICONS: Record<ExpenseCategoryKey, LucideIcon> = {
  inventory_stock: ShoppingCart,
  car_transport: Car,
  travel_lodging: Plane,
  lunch: Utensils,
  dining_out: UtensilsCrossed,
  entertainment_gifts: Sparkles,
  office_supplies: FileText,
  rent_premises: House,
  utilities: Zap,
  phone_internet: Phone,
  marketing_ads: Megaphone,
  software_subscriptions: Cpu,
  insurance: Shield,
  professional_services: Briefcase,
  training_education: GraduationCap,
  staff_costs: Users,
  bank_interest: Landmark,
  capital_asset: Building,
  other: Plus,
};

/**
 * FR-EXP-001b's category, asked BEFORE the file is sent.
 *
 * Opened when receipts are added, so each one is filed the moment it arrives
 * rather than sitting as an unfiled draft until somebody opens its form. One
 * choice covers everything added together: a drop of five files is five
 * receipts of one kind, and asking five times would be the reason nobody drops
 * five files.
 *
 * Choosing a tile answers immediately - there is no second "Confirm" tap
 * standing between a receipt and being filed. Closing (the X, Escape or a click
 * outside) adds nothing: nothing is queued that the person did not file.
 *
 * The list is `EXPENSE_CATEGORIES` and the server refuses a key that is not on
 * it; the account number and BTW shown are what that category is meant to book
 * to, not something this screen decides.
 */
export function CategoryPicker({
  fileCount,
  onPick,
  onCancel,
}: {
  fileCount: number;
  onPick: (key: ExpenseCategoryKey) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);

  // On the document rather than on the dialog: a key handler on a
  // `role="dialog"` element is what jsx-a11y rules out, and focus is trapped
  // inside the dialog anyway (`useModalFocus`), so the document sees every key.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  return (
    // The backdrop is a click-away convenience, not a control: the close button
    // and Escape are the keyboard routes, hence `presentation`.
    <div
      className="category-picker__scrim"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        className="category-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="category-picker-title"
        aria-describedby="category-picker-subtitle"
        data-testid="capture-category-picker"
      >
        <header className="category-picker__header">
          <span className="category-picker__mark" aria-hidden="true">
            <ScanLine size={22} strokeWidth={1.75} />
          </span>
          <div className="category-picker__heading">
            <h2 id="category-picker-title">{t("capture.category.title")}</h2>
            <p id="category-picker-subtitle">{t("capture.category.subtitle")}</p>
            {fileCount > 1 ? (
              <p className="category-picker__count" data-testid="capture-category-count">
                {t("capture.category.applies_to", { count: fileCount })}
              </p>
            ) : null}
          </div>
        </header>

        <ul className="category-picker__grid">
          {EXPENSE_CATEGORIES.map((category) => {
            const Glyph = ICONS[category.key];
            return (
              <li key={category.key}>
                <button
                  type="button"
                  className="category-picker__option"
                  data-testid={`capture-category-${category.key}`}
                  onClick={() => onPick(category.key)}
                >
                  <span className="category-picker__glyph" aria-hidden="true">
                    <Glyph size={18} strokeWidth={1.75} />
                  </span>
                  <span className="category-picker__text">
                    <span className="category-picker__name">
                      {t(`capture.category.${category.key}`)}
                    </span>
                    <span className="category-picker__meta">
                      {t("capture.category.meta", {
                        rgs: category.rgsCode,
                        vat: t(`capture.vat.${category.vatTreatment}`),
                      })}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>

        {/* After the grid in the DOM so focus lands on a category, not on
            "close"; drawn in the top corner by the stylesheet. */}
        <button
          type="button"
          className="category-picker__close"
          aria-label={t("capture.category.close")}
          data-testid="capture-category-cancel"
          onClick={onCancel}
        >
          <X size={18} strokeWidth={1.75} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
