import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";

import { meFixture, testAdministration } from "../testing/session";
import { OrganizationSettings } from "./SettingsScreens";

/**
 * FR-AR-003: an invoice cannot be issued without the seller's street, postcode
 * and city, and its warning says to add them in the administration's details.
 * Until this form there was nowhere in the app to do that.
 */

const refresh = vi.fn();
const updateAdministration = vi.fn(async () => ({}));

let administration = { ...testAdministration };

vi.mock("../session/SessionProvider", () => ({
  useSession: () => ({ me: meFixture(), administrations: [administration], refresh }),
}));
vi.mock("../session/ServicesProvider", () => ({
  useServices: () => ({ onboarding: { updateAdministration } }),
}));

function open() {
  render(
    <I18nProvider initialLanguage="en">
      <OrganizationSettings />
    </I18nProvider>,
  );
}

const field = (name: string) => screen.getByTestId(`administration-${name}`) as HTMLInputElement;

describe("the business address on Settings", () => {
  it("shows the five address fields, empty until entered, with the country defaulted", () => {
    administration = { ...testAdministration };
    open();
    expect(field("address-line1").value).toBe("");
    expect(field("address-line2").value).toBe("");
    expect(field("postal-code").value).toBe("");
    expect(field("city").value).toBe("");
    expect(field("country").value).toBe("NL");
    expect((screen.getByTestId("administration-save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("saves only the address fields that changed, trimmed", async () => {
    administration = { ...testAdministration };
    updateAdministration.mockClear();
    open();

    fireEvent.change(field("address-line1"), { target: { value: "  Keizersgracht 100 " } });
    fireEvent.change(field("postal-code"), { target: { value: "1015 AB" } });
    fireEvent.change(field("city"), { target: { value: "Amsterdam" } });
    fireEvent.click(screen.getByTestId("administration-save"));

    await waitFor(() => expect(updateAdministration).toHaveBeenCalledTimes(1));
    expect(updateAdministration).toHaveBeenCalledWith("adm-A", {
      address_line1: "Keizersgracht 100",
      postal_code: "1015 AB",
      city: "Amsterdam",
    });
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("clears a saved field when it is emptied, and never sends a blank country", async () => {
    administration = {
      ...testAdministration,
      address_line1: "Keizersgracht 100",
      address_line2: "Unit 4",
      postal_code: "1015 AB",
      city: "Amsterdam",
    };
    updateAdministration.mockClear();
    open();
    expect(field("address-line2").value).toBe("Unit 4");

    fireEvent.change(field("address-line2"), { target: { value: "  " } });
    fireEvent.click(screen.getByTestId("administration-save"));

    await waitFor(() => expect(updateAdministration).toHaveBeenCalledTimes(1));
    expect(updateAdministration).toHaveBeenCalledWith("adm-A", { address_line2: null });
  });
});
