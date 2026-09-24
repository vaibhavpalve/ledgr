"""Drive LEDGR's golden path over real HTTP against a running stack.

    python scripts/golden_path.py [--base http://127.0.0.1:8000]

Signs up a brand-new business, enrols TOTP, verifies the e-mail address through
the dev outbox (EXPOSE_DEV_OUTBOX=true), onboards an administration, creates a
customer, drafts and issues an invoice, captures and posts a receipt, and reads
the dashboard, ledger and reports back. Every step prints what it did; the first
failure stops the run with the response body, so the output is a list of exactly
where a real customer would get stuck.

Uses only the public API - no database access, no test fixtures - so a pass here
means a person clicking through the web app could do the same.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import struct
import sys
import time
import uuid
from datetime import date
from typing import Any

import httpx

PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class Stop(Exception):
    pass


def totp(secret: str, at: float | None = None) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


class Driver:
    def __init__(self, base: str) -> None:
        self.client = httpx.Client(base_url=base, timeout=60)
        self.token: str | None = None
        self.steps = 0

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 201),
        label: str,
    ) -> Any:
        self.steps += 1
        all_headers = {"Accept-Language": "en"}
        if self.token:
            all_headers["Authorization"] = f"Bearer {self.token}"
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            all_headers["Idempotency-Key"] = str(uuid.uuid4())
        all_headers.update(headers or {})
        response = self.client.request(
            method, path, json=json_body, content=content, headers=all_headers
        )
        ok = response.status_code in expect
        print(f"{'ok ' if ok else 'FAIL'} {self.steps:>2}. {label}  [{method} {path} -> {response.status_code}]")
        if not ok:
            print(response.text[:2000])
            raise Stop(label)
        if not response.content:
            return None
        if "json" not in response.headers.get("content-type", ""):
            return response.content
        return response.json()


def run(base: str) -> int:
    d = Driver(base)
    today = date.today()
    email = f"golden-{uuid.uuid4().hex[:8]}@example.nl"
    try:
        signup = d.call(
            "POST",
            "/v1/auth/signup",
            json_body={
                "account_model": "self_managed",
                "organization_name": "Golden Path B.V.",
                "email": email,
                "password": "correct-horse-battery-staple-42",
            },
            label="sign up a new business",
        )
        d.token = signup["access_token"]

        begin = d.call("POST", "/v1/auth/mfa/totp/enroll/begin", label="begin TOTP enrolment")
        confirmed = d.call(
            "POST",
            "/v1/auth/mfa/totp/enroll/confirm",
            json_body={"secret": begin["secret"], "code": totp(begin["secret"])},
            label="confirm TOTP",
        )
        d.token = confirmed["access_token"]

        outbox = d.call("GET", "/v1/dev/outbox", label="read the dev outbox")
        messages = outbox if isinstance(outbox, list) else outbox.get("messages", [])
        mine = [m for m in messages if email in json.dumps(m)]
        link_token = None
        for message in mine:
            text = json.dumps(message)
            marker = "token="
            if marker in text:
                link_token = text.split(marker, 1)[1][:36]
        if link_token is None:
            raise Stop(f"no verification link for {email} in the outbox")
        d.call(
            "POST",
            "/v1/auth/verify-email",
            json_body={"token": link_token},
            label="verify the e-mail address",
        )

        me = d.call("GET", "/v1/me", label="read /v1/me")
        assert me["onboarding"]["needs_administration"] is True, me["onboarding"]

        year = today.year
        admin = d.call(
            "POST",
            "/v1/administrations",
            json_body={
                "legal_name": "Golden Path B.V.",
                "trade_name": None,
                "legal_form": "BV",
                "kvk_number": "34281907",
                "vat_number": "NL001234567B01",
                "formatting_locale": "nl-NL",
                "fiscal_year": {
                    "start_date": f"{year}-01-01",
                    "end_date": f"{year}-12-31",
                    "period_scheme": "quarterly",
                },
            },
            label="onboard: create the administration",
        )
        admin_id = admin["id"]
        fiscal_year_id = admin["fiscal_years"][0]["id"]
        base_path = f"/v1/administrations/{admin_id}"

        d.call(
            "PATCH",
            base_path,
            json_body={
                "address_line1": "Keizersgracht 100",
                "postal_code": "1015 AB",
                "city": "Amsterdam",
                "iban": "NL91ABNA0417164300",
            },
            label="settings: seller address and IBAN",
        )

        customer = d.call(
            "POST",
            f"{base_path}/customers",
            json_body={
                "name": "Klant B.V.",
                "address_line1": "Damrak 1",
                "postal_code": "1012 LG",
                "city": "Amsterdam",
                "country": "NL",
                "invoice_email": "klant@example.nl",
                "payment_terms_days": 14,
            },
            label="create a customer",
        )

        invoice = d.call(
            "POST",
            f"{base_path}/sales-invoices",
            json_body={
                "fiscal_year_id": fiscal_year_id,
                "invoice_date": today.isoformat(),
                "customer_id": customer["id"],
                "lines": [
                    {
                        "description": "Consultancy",
                        "quantity": "10",
                        "unit_price": "95.00",
                        "vat_treatment": "btw_21",
                    }
                ],
            },
            label="draft an invoice",
        )
        issued = d.call(
            "POST",
            f"{base_path}/sales-invoices/{invoice['id']}/issue",
            label="issue the invoice (posts to the ledger)",
        )
        print(f"      invoice {issued.get('invoice_reference') or issued.get('invoice_number')}")
        document_id = issued.get("document_id")
        if not document_id:
            raise Stop("the issued invoice has no PDF document")
        pdf = d.call(
            "GET",
            f"{base_path}/documents/{document_id}/content",
            expect=(200,),
            label="download the invoice PDF",
        )
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF"):
            raise Stop("the invoice document is not a PDF")

        sitting = d.call("POST", f"{base_path}/capture-sessions", label="open a capture sitting")
        page = d.call(
            "POST",
            f"{base_path}/capture-sessions/{sitting['id']}/pages"
            f"?fiscal_year_id={fiscal_year_id}&source=upload&filename=bon.pdf",
            content=PDF,
            headers={"Content-Type": "application/pdf"},
            label="upload a receipt",
        )
        expense_id = page.get("expense_id") or page.get("expense", {}).get("id")
        if expense_id is None:
            listed = d.call("GET", f"{base_path}/expenses", label="list expenses")
            rows = listed if isinstance(listed, list) else listed.get("expenses", [])
            expense_id = rows[0]["id"]
        d.call(
            "PATCH",
            f"{base_path}/expenses/{expense_id}",
            json_body={
                "expense_date": today.isoformat(),
                "supplier": "Staples",
                "gross_amount": "121.00",
                "vat_treatment": "btw_21",
                "category": "Office supplies",
                "payment_method": "business_account",
            },
            label="fill in the receipt",
        )
        d.call("POST", f"{base_path}/expenses/{expense_id}/ready", label="mark the receipt ready")
        d.call(
            "POST",
            f"{base_path}/expenses/{expense_id}/posting",
            label="post the receipt to the ledger",
        )

        dashboard = d.call(
            "GET",
            f"{base_path}/dashboard?fiscal_year_id={fiscal_year_id}",
            label="read the dashboard",
        )
        print(
            f"      cash {dashboard.get('cash_position')} receivables {dashboard.get('receivables')}"
            f" vat {dashboard.get('vat_estimate')}"
        )
        d.call(
            "GET",
            f"{base_path}/trial-balance?fiscal_year_id={fiscal_year_id}",
            label="read the trial balance",
        )
        d.call(
            "GET",
            f"{base_path}/reports/income-statement?fiscal_year_id={fiscal_year_id}",
            label="read the income statement",
        )
        returns = d.call(
            "GET",
            f"{base_path}/vat-returns?fiscal_year_id={fiscal_year_id}",
            label="read the BTW returns of the year",
        )["returns"]
        current = next(
            r for r in returns if r["start_date"] <= today.isoformat() <= r["end_date"]
        )
        vat_return = d.call(
            "GET",
            f"{base_path}/vat-returns/{current['period_id']}",
            label="open this period's BTW return",
        )
        boxes = {box["code"]: box for box in vat_return["boxes"]}
        # 10 x 95.00 at 21% on the invoice, 121.00 gross at 21% on the receipt.
        expected = {
            ("1a", "turnover"): "950.00",
            ("1a", "vat"): "199.50",
            ("5b", "vat"): "21.00",
        }
        for (code, column), amount in expected.items():
            actual = boxes[code][column]
            if actual != amount:
                raise Stop(f"box {code} {column} is {actual}, expected {amount}")
        print(
            f"      1a {boxes['1a']['turnover_rounded']} / {boxes['1a']['vat_rounded']}, "
            f"5b {boxes['5b']['vat_rounded']}, total due {vat_return['total_due']}"
        )
        lines = d.call(
            "GET",
            f"{base_path}/vat-returns/{current['period_id']}/boxes/1a/lines",
            label="drill 1a down to its postings",
        )["lines"]
        if {line["amount"] for line in lines} != {"950.00", "199.50"}:
            raise Stop(f"1a's postings do not reconcile: {lines}")
    except Stop as stop:
        print(f"\nSTOPPED at: {stop}")
        return 1
    print(f"\nGolden path complete: {d.steps} calls, account {email}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    sys.exit(run(parser.parse_args().base))
