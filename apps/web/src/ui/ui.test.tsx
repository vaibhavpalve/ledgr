import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "@ledgr/i18n";

import { axeViolations } from "../testing/axe";
import {
  Amount,
  Badge,
  Button,
  CashChart,
  ClientAvatar,
  DataTable,
  EmptyState,
  Field,
  initialsOf,
  KpiCard,
  NavItem,
  toneFor,
  type Column,
} from "./index";
import { House } from "lucide-react";

function wrap(node: React.ReactNode) {
  return render(
    <I18nProvider initialLanguage="en">
      <MemoryRouter>{node}</MemoryRouter>
    </I18nProvider>,
  );
}

describe("Amount (NFR-031, DESIGN.md section 5)", () => {
  it("formats nl-NL from the exact decimal string", () => {
    wrap(<Amount value="1250.00" />);
    expect(screen.getByText(/1\.250,00/)).not.toBeNull();
  });

  it("writes a negative with the real minus sign U+2212, not a hyphen", () => {
    wrap(<Amount value="-52.80" />);
    const text = screen.getByText(/52,80/).textContent ?? "";
    expect(text).toContain("−");
    expect(text).not.toContain("-");
  });

  it("is danger-coloured only when told it is overdrawn", () => {
    wrap(<Amount value="10.00" />);
    expect(screen.getByText(/10,00/).classList.contains("ui-amount--overdrawn")).toBe(false);
  });
});

describe("ClientAvatar", () => {
  it("takes two initials and gives an id a stable tone", () => {
    expect(initialsOf("Datapal BV")).toBe("DB");
    expect(initialsOf("Studio")).toBe("ST");
    expect(toneFor("abc")).toBe(toneFor("abc"));
  });
});

describe("DataTable", () => {
  interface Line {
    id: string;
    name: string;
    debit: string;
  }
  const columns: Column<Line>[] = [
    { key: "name", header: "Account", render: (r) => r.name },
    { key: "debit", header: "Debit", numeric: true, render: (r) => <Amount value={r.debit} /> },
  ];

  it("right-aligns numeric columns and marks the selected row", () => {
    wrap(
      <DataTable
        caption="Lines"
        columns={columns}
        rows={[{ id: "1", name: "Bank", debit: "5.00" }]}
        rowKey={(r) => r.id}
        selectedKey="1"
      />,
    );
    const table = screen.getByRole("table", { name: "Lines" });
    expect(
      within(table).getByRole("columnheader", { name: "Debit" }).classList.contains("ui-num"),
    ).toBe(true);
    expect(within(table).getAllByRole("row")[1]?.getAttribute("aria-selected")).toBe("true");
  });
});

describe("Field", () => {
  it("links the label, and an error is announced with a sentence", () => {
    wrap(<Field label="Email" error="Enter an email address like name@company.nl" />);
    const input = screen.getByLabelText("Email");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("Enter an email address");
  });
});

describe("CashChart", () => {
  const series = [
    { label: "Apr", value: "14210.00" },
    { label: "May", value: "16400.50" },
    { label: "Sep", value: "24310.45" },
  ];

  it("has a text summary, a table alternative and marks the last point", () => {
    wrap(<CashChart series={series} summary="Cash position rose from April to September" />);
    expect(screen.getByRole("img", { name: /rose from April/ })).not.toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(document.querySelector(".ui-chart__tip")?.textContent).toMatch(/Sep · .*24\.310,45/);
  });

  it("moves the tooltip when a column is hovered", () => {
    wrap(<CashChart series={series} summary="s" />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /Apr/ }));
    expect(document.querySelector(".ui-chart__tip")?.textContent).toMatch(/Apr · .*14\.210,00/);
  });
});

describe("the primitives together", () => {
  it("has no axe violations (CMP-012)", async () => {
    const { container } = wrap(
      <main>
        <h1>Home</h1>
        <nav aria-label="Main">
          <NavItem to="/" end icon={House}>
            Home
          </NavItem>
        </nav>
        <Button variant="primary">Save</Button>
        <Badge variant="booked">Booked</Badge>
        <ClientAvatar name="Datapal BV" />
        <KpiCard label="Cash position" value={<Amount value="0.00" />} caption="No bookings yet" />
        <EmptyState title="Nothing yet">Capture a receipt to begin.</EmptyState>
        <Field label="Email" helper="We never share it" />
      </main>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});
