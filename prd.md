# Product Requirements Document — Cloud Bookkeeping Platform

**Working name:** LEDGR
**Version:** 1.2
**Status:** Draft for review
**Date:** 19 August 2026
**Owner:** Product
**Classification:** Internal

---

## 0. Document control

| Field | Value |
|---|---|
| Document type | Product Requirements Document |
| Scope | Web application, iOS app, Android app, public API |
| Primary market | Netherlands, phase 2 Belgium and Germany |
| Regulatory anchor | Dutch fiscal law, EU GDPR, EU ViDA |
| Review cadence | Every sprint until GA, then quarterly |
| Languages | Dutch and English, both first-class |

### Changes in v1.2

- Receipt capture by camera and upload, with expense posting, moved into P0 (§6.7).
- Sales invoicing moved into P0, with a new §6.3.1 covering the invoice designer: logo, typography, colour, layout, columns and content blocks.
- P0 reframed from an internal-only foundation to a shippable first version for design partners; a mobile client is therefore in P0 scope (Q13).
- Split made explicit: P0 delivers capture and manual confirmation, P1 delivers confidence-scored AI extraction.

### Changes in v1.1

- Both account models — firm-led and self-managed — moved into P0; open question Q1 closed.
- New §5.1 defining the two account models and the transitions between them.
- New §8.6 covering firm control of client access, including a client rights floor that no firm setting can remove.
- Google sign-in (OIDC) added to §8.2, with account-linking and lockout-prevention requirements.
- Language selection before login; Dutch and English elevated to a P0 requirement in §20.
- New §7.1 and §7.2 covering design principles and measurable usability requirements.

### Requirement ID convention

| Prefix | Domain |
|---|---|
| `FR-<module>-nnn` | Functional requirement |
| `NFR-nnn` | Non-functional requirement |
| `IAM-nnn` | Identity and access management |
| `SEC-nnn` | Security |
| `PRIV-nnn` | Privacy and data protection |
| `CMP-nnn` | Regulatory compliance |
| `MOB-nnn` | Mobile-specific |

Priority uses MoSCoW: **M** must, **S** should, **C** could, **W** won't (this release).

---

## 1. Summary

LEDGR is a multi-tenant cloud bookkeeping platform for Dutch small and medium businesses and the accounting firms that serve them. It automates the path from source document to filed tax return: documents arrive by e-invoice, email or photo, are extracted and coded automatically, matched against bank transactions, and roll up into VAT returns and annual accounts filed straight to the Belastingdienst and KvK.

It competes with Exact Online, Yuki, Twinfield, AFAS, Moneybird and e-Boekhouden. The wedge is automation depth combined with a genuine mobile product — incumbents treat mobile as a receipt-scanner bolt-on.

The platform ships as a responsive web app, a native-quality iOS app, a native-quality Android app, and a documented public API. All three clients run against one API.

---

## 2. Problem and opportunity

### 2.1 Problem

Dutch SMB bookkeeping is still largely manual. An entrepreneur photographs receipts into a folder, a bookkeeper re-keys them, VAT returns are assembled quarterly under time pressure, and the accountant discovers errors months later during year-end. Accounting firms carry the cost: staff time spent on data entry they cannot bill at advisory rates, in a market with a structural shortage of qualified staff.

Existing tools solve parts of this. Exact Online is broad but heavy and expensive for micro-businesses. Yuki automates well but locks the client into a firm-mediated model. Moneybird is pleasant but shallow for anything beyond invoicing. None of them offers a mobile experience an owner would actually run their books from.

### 2.2 Opportunity

Three forces make now the right time:

1. **ViDA.** The EU VAT in the Digital Age directive mandates structured e-invoicing and digital reporting for intra-Community B2B from 1 July 2030. The Netherlands has signalled a Peppol-based domestic B2B mandate on a comparable timeline, with draft legislation expected for consultation. Every Dutch business will need EN 16931-capable software within this decade. Incumbents will retrofit; a new entrant can be structured-invoice-native from day one.
2. **RGS maturity.** RGS 3.8 with NT20 mappings makes automated classification and one-click SBR reporting achievable without bespoke per-client chart-of-accounts work.
3. **Document AI.** Extraction accuracy on invoices and receipts is now high enough that automatic coding, not just automatic capture, is a defensible product claim.

### 2.3 Non-goals

Explicitly out of scope for this product:

- Payroll calculation and wage tax filing. Integrate with Nmbrs, Loket or AFAS instead.
- ERP functions: manufacturing, MRP, warehouse management, POS.
- Acting as a licensed payment institution. Payment initiation goes through a licensed PISP partner.
- Personal income tax return preparation for individuals unrelated to a business.
- Statutory audit tooling for large entities.

---

## 3. Goals and success metrics

### 3.1 Product goals

| # | Goal |
|---|---|
| G1 | An SMB owner can complete a full quarter of bookkeeping and file a correct VAT return without an accountant. |
| G2 | An accounting firm can service a client portfolio with materially less manual entry per client than today. |
| G3 | Every posting is traceable to a source document and an actor, permanently. |
| G4 | The platform is auditable and defensible under a Belastingdienst inspection. |
| G5 | Access to customer financial data is minimal, scoped, time-bound and logged — by default, not by configuration. |
| G6 | A ZZP with no accounting training can complete their core tasks without reading documentation or being trained. Easy to use is the product's defining constraint, not a polish item. |

### 3.2 Success metrics

| Metric | Target at 12 months post-GA |
|---|---|
| Straight-through processing rate (purchase invoices posted with no human edit) | ≥ 70% |
| Bank transaction auto-match rate | ≥ 85% |
| Median time from document upload to posted entry | < 60 seconds |
| VAT returns filed with zero post-filing correction (suppletie) | ≥ 97% |
| Monthly active mobile users / total active users | ≥ 45% |
| Onboarding completion (signup to first posted transaction) | ≥ 60% within 7 days |
| Gross revenue retention | ≥ 92% |
| P1 security incidents | 0 |

### 3.3 Anti-metrics

Watch for gaming: auto-match rate rising while correction volume also rises means the matcher is being over-confident. Track **auto-posted entries later reversed** as a paired quality metric, target < 2%.

---

## 4. Users and personas

| Persona | Description | Primary needs |
|---|---|---|
| **Sanne — ZZP / freelancer** | Sole trader, no bookkeeping training, uses phone as primary device | Photograph receipt, send invoice, know what VAT to set aside, file OB return |
| **Marco — SMB owner** | BV with 8 staff, delegates bookkeeping, wants monthly numbers | Dashboard, cash position, approve payments, delegate without giving full access |
| **Fatima — in-house bookkeeper** | Processes purchase invoices, reconciles bank, prepares VAT | Fast keyboard-driven entry, bulk actions, clear exception queues |
| **Peter — accountant at firm** | Manages 60+ client administrations | Portfolio view, cross-client work queue, year-end close, SBR filing, no per-client re-learning |
| **Iris — firm office manager** | Manages staff, licences, client onboarding | User provisioning, role assignment, access review, billing |
| **Auditor / Belastingdienst inspector** | External, occasional, read-only | Complete audit trail, XAF export, immutable history |
| **LEDGR support engineer** | Internal | Time-boxed, approved, logged access to diagnose a specific tenant issue |

---

## 5. Scope and release plan

### 5.1 Account models

Two ways in, one product underneath. Both ship in **P0**. The difference is who owns the organization and who controls access — not which features exist.

**Model A — Firm-led.** An accounting or administration office signs up as an organization. It creates or is invited into client administrations, each belonging to a ZZP, eenmanszaak, VOF, BV, stichting or vereniging. Firm staff work across many clients. The firm decides what each client's own users can see and do in their administration.

**Model B — Self-managed.** A ZZP or BV signs up directly and runs its own books. The organization has one administration (or several, for a holding structure). No firm is involved unless the business invites one later.

| ID | Requirement | Pri |
|---|---|---|
| FR-MDL-001 | Signup asks one question — "I keep books for my own business" or "I keep books for clients" — and branches into Model B or Model A. No other choice is required to get started. | M |
| FR-MDL-002 | A firm organization can hold an unlimited number of client administrations, each with its own legal entity data, fiscal calendar, chart of accounts, VAT regime and document archive. | M |
| FR-MDL-003 | A firm user switches between client administrations without re-authenticating, through a searchable switcher that shows only administrations they are granted. | M |
| FR-MDL-004 | A firm can create a client administration on behalf of a client who has no account yet, then invite the client's own users into it. | M |
| FR-MDL-005 | A firm can invite an existing self-managed business to grant it access; the business accepts or declines. A firm never gains access to an existing administration without the client's acceptance. | M |
| FR-MDL-006 | The firm controls the client-side permission profile for each administration it manages — see §8.6. | M |
| FR-MDL-007 | A self-managed business can invite an accountant at any time, granting scoped access; from that point the administration is jointly worked but ownership does not transfer. | M |
| FR-MDL-008 | Transition in both directions without data migration: a self-managed business can hand an administration to a firm, and a firm can hand one back. Ownership transfer requires acceptance by both sides and is logged. | M |
| FR-MDL-009 | The client always retains the right to revoke firm access and export their complete data, without the firm's cooperation and without contacting support. This right cannot be disabled by any firm setting. | M |
| FR-MDL-010 | Billing is independent of the access model: a firm may be billed for client administrations, or the client may be billed directly, per administration. | M |

### 5.2 Release phases

P0 is no longer an internal-only foundation. It now contains the two things a business actually judges a bookkeeping product on in week one — getting a receipt in, and getting an invoice out — so it ships to design partners as a usable first version.

| Phase | Name | Content | Gate |
|---|---|---|---|
| **P0** | Foundation + first usable version | Both account models (firm-led and self-managed), tenancy, IAM including firm-controlled client access, Google sign-in, EN/NL bilingual UI with pre-login language selection, audit log, ledger engine, chart of accounts, manual journals, security baseline, **receipt capture by camera and upload with expense posting**, **sales invoicing with a customisable invoice designer (logo, fonts, layout, colours)** | Design partners, ~20 businesses |
| **P1** | MVP | AI extraction on captured documents, purchase invoice workflow and approvals, bank feeds, reconciliation, VAT return, firm portfolio dashboard, recurring invoicing, dunning | Closed beta, 15 firms and 25 direct businesses |
| **P2** | GA | Peppol send/receive, full mobile apps in both stores, full accountant portal, reporting, fixed assets, public API | Public launch |
| **P3** | Scale | Multi-entity, multi-currency, consolidation, SBR annual accounts to KvK, ICP/OSS, SSO/SCIM, marketplace | — |
| **P4** | Expand | Belgium and Germany localisation, ViDA digital reporting layer, forecasting and advisory tooling | — |

**Consequence to accept deliberately:** camera capture in P0 means a mobile client exists in P0. The cheapest route is an installable PWA with camera access, with the native apps following in P2 for offline queueing, biometrics and push. If native-from-day-one is preferred instead, P0 lengthens by roughly a release cycle. See Q13.

**Split to be clear about:** P0 delivers capture, storage and posting — the user photographs a receipt, confirms amount, VAT and category, and it posts. P0 does *not* promise AI extraction accuracy; the fields may be pre-filled on a best-effort basis but manual entry is always a complete path. Automated extraction with confidence scoring is P1.
| **P3** | Scale | Multi-entity, multi-currency, consolidation, SBR annual accounts to KvK, ICP/OSS, SSO/SCIM, marketplace | — |
| **P4** | Expand | Belgium and Germany localisation, ViDA digital reporting layer, forecasting and advisory tooling | — |

### 5.3 In scope for GA (P2)

Ledger and journals; sales invoicing including Peppol; purchase invoice capture and approval; bank connectivity and reconciliation; VAT (OB) return preparation and filing; expenses and mileage; fixed assets; standard reporting; document archive; accountant portal; web, iOS, Android; public REST API; full IAM, security and privacy controls in this document.

### 5.4 Deferred past GA

Multi-currency revaluation, consolidation, budgeting, project accounting, inventory, ICP/OSS returns, corporate income tax (VPB) pre-return, KvK annual accounts deposit, SSO/SCIM, non-NL jurisdictions.

---

## 6. Functional requirements

### 6.1 Onboarding and company setup — `FR-ONB`

| ID | Requirement | Pri |
|---|---|---|
| FR-ONB-000 | Language (Dutch or English) is selectable on the signup and login screens before any account exists, defaulting to browser/device locale. | M |
| FR-ONB-001 | Self-service signup via Google, passkey or email + password, with mandatory MFA enrolment before first financial data is entered. Google signup skips email verification only because the identity provider has already verified it (IAM-010b). | M |
| FR-ONB-001a | Signup branches on one question: keeping books for my own business (Model B) or for clients (Model A). The branch is reversible in the first 30 days without support. | M |
| FR-ONB-001b | Firm signup path additionally captures firm name, KvK number and optional SBR/Digipoort credentials, then lands on an empty client portfolio with a single prominent "Add your first client" action. | M |
| FR-ONB-001c | A client invited by a firm signs up through an invitation link that pre-fills their administration and their permitted role; they never re-enter company data the firm already provided. | M |
| FR-ONB-002 | Company lookup by KvK number or name, pre-filling legal name, address, legal form and SBI code from the KvK API. | M |
| FR-ONB-003 | Capture and validate BTW-identificatienummer and OB-nummer; validate EU VAT numbers via VIES. | M |
| FR-ONB-004 | Legal form selection (eenmanszaak, VOF, BV, stichting, vereniging) drives the default chart of accounts and reporting taxonomy. | M |
| FR-ONB-005 | Chart of accounts seeded from the RGS MKB profile matching the legal form; user may extend but not break RGS mapping integrity. | M |
| FR-ONB-006 | Fiscal year definition, including non-calendar and short first years. | M |
| FR-ONB-007 | VAT regime setup: quarterly/monthly filing frequency, KOR participation, OSS registration, reverse-charge applicability. | M |
| FR-ONB-008 | Opening balance entry via guided wizard, trial balance import (CSV/XLSX), or XAF import from a previous package. | M |
| FR-ONB-009 | Migration import from Exact Online, Moneybird, e-Boekhouden and XAF 3.2 audit files, covering masters, open items and history. | S |
| FR-ONB-010 | Onboarding is resumable across devices and sessions; partial state is never lost. | M |
| FR-ONB-011 | Sandbox/demo administration with synthetic data, clearly labelled, never included in reporting or filings. | S |

### 6.2 General ledger and accounting engine — `FR-GL`

| ID | Requirement | Pri |
|---|---|---|
| FR-GL-001 | Double-entry ledger. No transaction may persist unless total debits equal total credits in the administration's functional currency. | M |
| FR-GL-002 | Journal types: sales, purchase, bank, cash, memorial, opening, closing. Each posting belongs to exactly one journal and one period. | M |
| FR-GL-003 | Postings are immutable once committed. Corrections are made by reversing entry, never by mutation or deletion. | M |
| FR-GL-004 | Every posting carries: administration, fiscal year, period, journal, date, description, document reference, actor, source system, timestamp. | M |
| FR-GL-005 | Chart of accounts with account type (asset, liability, equity, revenue, expense), RGS reference code, VAT default, and blocked/active state. | M |
| FR-GL-006 | Sub-ledgers for accounts receivable and accounts payable, reconciled to control accounts continuously; a control account cannot be posted to directly. | M |
| FR-GL-007 | Period locking per fiscal period with a defined unlock authority; VAT-filed periods are hard-locked and require a suppletie flow to change. | M |
| FR-GL-008 | Year-end close: result appropriation to equity, opening balance generation for the next year, closing journal, and re-openable prior year with cascade recalculation. | M |
| FR-GL-009 | Cost centres and cost units (kostenplaatsen / kostendragers) as optional posting dimensions. | S |
| FR-GL-010 | Multi-currency: transaction currency, functional currency, daily ECB rates, realised and unrealised exchange differences. | S (P3) |
| FR-GL-011 | Recurring journal templates with schedule and auto-post or auto-draft option. | S |
| FR-GL-012 | Accruals and deferrals with automatic release schedules over defined periods. | S |
| FR-GL-013 | Numbering sequences per journal and per year, gapless, with gap detection reporting. | M |

### 6.3 Sales invoicing and accounts receivable — `FR-AR`

| ID | Requirement | Pri |
|---|---|---|
| FR-AR-001 | Create, edit, send and credit sales invoices with line items, quantities, unit prices, discounts and per-line VAT treatment. | M (P0) |
| FR-AR-002 | VAT handling: 21%, 9%, 0%, exempt (vrijgesteld), reverse charge domestic (verlegd), intra-Community supply, export, margin scheme. Correct legal wording rendered per treatment. | M |
| FR-AR-003 | Invoices comply with Dutch statutory invoice content requirements; the system blocks sending if a mandatory field is absent. | M |
| FR-AR-004 | Sequential, gapless invoice numbering per year, configurable prefix; issued numbers cannot be reused. | M (P0) |
| FR-AR-005 | Send as PDF by email in P0; structured e-invoice over Peppol (BIS Billing 3.0 / NLCIUS, EN 16931) added in P2. Delivery status tracked per channel. | M |
| FR-AR-006 | Customer master with KvK number, VAT number, Peppol participant ID discovery, payment terms, credit limit, preferred delivery channel and language. | M |
| FR-AR-007 | Quotes and order confirmations convertible to invoices. | S |
| FR-AR-008 | Recurring/subscription invoicing with schedules, indexation and end dates. | S |
| FR-AR-009 | Payment links on invoices via iDEAL, SEPA direct debit and card, through a licensed PSP; incoming payment auto-matches to the invoice. | M |
| FR-AR-010 | Dunning: configurable reminder ladder with escalation, statutory interest and collection cost calculation, and pause-per-customer. | M |
| FR-AR-011 | SEPA direct debit mandate management including mandate reference, signature date, first/recurring flag and pain.008 file generation. | S |
| FR-AR-012 | Aged receivables reporting and per-customer statement of account. | M |
| FR-AR-013 | Bad debt write-off with the associated VAT reclaim entry. | S |


#### 6.3.1 Invoice designer and branding — `FR-TPL` (P0)

The invoice is the client-facing artefact a business is judged on. A generic template with someone else's aesthetic is a reason not to adopt the product, which is why this sits in P0 rather than being deferred as polish.

| ID | Requirement | Pri |
|---|---|---|
| FR-TPL-001 | Logo upload (PNG, JPEG, SVG), with position (left, centre, right), size control and a transparent-background preview against the paper colour. | M (P0) |
| FR-TPL-002 | Typography: a curated set of at least 8 licensed, PDF-embeddable typefaces covering serif, sans and monospace, chosen for numeric legibility (tabular figures) and full Latin-1 coverage. Separate selection for headings, body and figures. | M (P0) |
| FR-TPL-003 | Type controls: size scale, weight, line height and letter spacing, exposed as a small number of sensible steps rather than free numeric entry, so a user cannot produce an unreadable invoice. | M (P0) |
| FR-TPL-004 | Colour: accent colour, text colour and background/paper colour, with an automatic contrast check that warns when a combination fails legibility on screen or in greyscale print. | M (P0) |
| FR-TPL-005 | Layout: at least 4 starting layouts (classic, modern, compact, minimal), each adjustable for header arrangement, logo/address block placement, line-item column selection and order, totals block position, and footer content. | M (P0) |
| FR-TPL-006 | Column control on line items: show, hide and reorder quantity, unit, unit price, discount, VAT rate, VAT amount and line total. Hiding a column never hides information the invoice is legally required to carry. | M (P0) |
| FR-TPL-007 | Editable content blocks: header text, intro text, payment terms, footer, and a free block for chamber of commerce number, VAT number, IBAN and general terms reference. Fields support merge tags (customer name, invoice number, due date, amounts). | M (P0) |
| FR-TPL-008 | Live preview updates as settings change, rendered from the same engine that produces the final PDF — what is previewed is what is sent. No separate preview renderer. | M (P0) |
| FR-TPL-009 | **Statutory fields cannot be removed or hidden.** The designer enforces the Dutch invoice content requirements (FR-AR-003): removing or obscuring a mandatory field is not offered as an option, and the template cannot be saved in a non-compliant state. The reason is shown inline, not as a generic error. | M (P0) |
| FR-TPL-010 | Page setup: A4 and US Letter, margins, page numbering, and correct multi-page behaviour with repeating headers and carried-forward subtotals. | M (P0) |
| FR-TPL-011 | Multiple named templates per administration, with one default, and per-customer or per-document-type template assignment. | S (P1) |
| FR-TPL-012 | Templates apply to the whole document family — invoice, credit note, quote, order confirmation, reminder and statement — so branding is set once. | M (P0) |
| FR-TPL-013 | Language of the rendered document follows the recipient, independent of the designer's UI language; a template holds both Dutch and English content for its text blocks. | M (P0) |
| FR-TPL-014 | Firms may define house templates and push them to managed client administrations, and may allow or lock client editing per §8.6. | S (P1) |
| FR-TPL-015 | Generated PDFs are PDF/A-3 compliant for archiving, with the structured e-invoice XML embedded once Peppol ships in P2. | M |
| FR-TPL-016 | Accessibility of output: tagged PDF structure, selectable text (never a rendered image), and a minimum effective body size of 9pt. | M |
| FR-TPL-017 | Sent invoices are immutable. Editing a template never alters the appearance of an already-issued invoice; the rendered PDF is stored as issued. | M (P0) |
| FR-TPL-018 | Uploaded logos are validated and sanitised — SVG is stripped of scripts and external references before storage or rendering (see SEC-005, SEC-006). | M (P0) |
| FR-TPL-019 | Reset to default, and duplicate an existing template, are both one action. Users experiment more when the way back is obvious. | M (P0) |
| FR-TPL-020 | Custom fonts uploaded by the user are **not** supported in P0; the licensing and embedding exposure is not worth carrying at this stage. Revisit at P3. | W |

### 6.4 Purchase invoices and accounts payable — `FR-AP`

| ID | Requirement | Pri |
|---|---|---|
| FR-AP-001 | Document intake via: web upload, mobile camera, drag-and-drop, dedicated per-administration email address, Peppol inbound, and supplier portal fetch. | M |
| FR-AP-002 | AI extraction of supplier, invoice number, dates, currency, net/VAT/gross amounts per rate, IBAN, payment reference, and line items — with a per-field confidence score. | M |
| FR-AP-003 | Duplicate detection across supplier + invoice number + amount + date, warning before posting. | M |
| FR-AP-004 | Automatic ledger coding proposal from supplier history, RGS category and line description; the model's proposal is always visible and editable. | M |
| FR-AP-005 | Inbound Peppol e-invoices bypass OCR and post from structured data, targeting straight-through processing. | M |
| FR-AP-006 | Configurable approval workflow: by amount threshold, cost centre, supplier or budget; multi-step; delegation during absence. | M |
| FR-AP-007 | Approval decisions are recorded with actor, timestamp, decision and comment, and are immutable. | M |
| FR-AP-008 | Payment batch creation producing SEPA pain.001 files, or initiation via a licensed PISP with strong customer authentication. | M |
| FR-AP-009 | Supplier master with IBAN, VAT number, default ledger account, default VAT code and payment terms. IBAN changes require re-verification and are flagged as a fraud risk event. | M |
| FR-AP-010 | Aged payables and cash requirement forecast. | M |
| FR-AP-011 | Purchase orders with three-way match against receipt and invoice. | C (P4) |
| FR-AP-012 | Reverse-charge and intra-Community acquisition VAT automatically posted on both sides. | M |

### 6.5 Banking and reconciliation — `FR-BNK`

| ID | Requirement | Pri |
|---|---|---|
| FR-BNK-001 | PSD2 account information access via a licensed AISP aggregator for all major NL and BE banks, with explicit user consent and 90/180-day consent renewal handling. | M |
| FR-BNK-002 | File-based import of CAMT.053, MT940 and CSV as a fallback and for banks without API coverage. | M |
| FR-BNK-003 | Automatic matching of transactions to open AR/AP items using amount, payment reference, IBAN, name similarity and historical behaviour, with a confidence score. | M |
| FR-BNK-004 | Confidence thresholds are configurable: auto-post above the high threshold, propose between thresholds, queue for manual handling below. Defaults are conservative. | M |
| FR-BNK-005 | Partial payments, overpayments, batched payments (one transaction covering many invoices) and split allocation. | M |
| FR-BNK-006 | Learning rules: a user correction updates the matching model for that tenant only, never across tenants. | M |
| FR-BNK-007 | Bank reconciliation statement per account per period, with an explicit unreconciled-difference figure that must be zero to close a period. | M |
| FR-BNK-008 | Cash book and petty cash journal with cash count support. | S |
| FR-BNK-009 | Credit card statement import and matching, including corporate card feeds. | S |
| FR-BNK-010 | Duplicate-transaction detection on re-import of overlapping statement periods. | M |

### 6.6 VAT and tax filing — `FR-VAT`

| ID | Requirement | Pri |
|---|---|---|
| FR-VAT-001 | VAT (omzetbelasting) return preparation for monthly, quarterly and annual filers, mapped to the official rubrieken (1a–5b). | M |
| FR-VAT-002 | Pre-filing validation: unposted documents in period, unreconciled bank items, unbalanced VAT control accounts, prior-period movements — each blocking or warning. | M |
| FR-VAT-003 | Electronic filing to the Belastingdienst via SBR/Digipoort using the current Nederlandse Taxonomie (NT20 or later), with receipt confirmation stored as evidence. | M |
| FR-VAT-004 | Digipoort connectivity uses the current supported interface; the integration is version-pinned with an owned upgrade path as Logius deprecates older endpoints. | M |
| FR-VAT-005 | Suppletie (correction return) flow for filed periods, with a clear link to the original filing. | M |
| FR-VAT-006 | ICP declaration (opgaaf intracommunautaire prestaties) with per-customer VAT number totals and VIES validation. | S (P3) |
| FR-VAT-007 | OSS / EU e-commerce VAT: country-level rate determination, threshold monitoring and OSS return preparation. | S (P3) |
| FR-VAT-008 | KOR (small business scheme) support including threshold monitoring and mid-year entry/exit. | S |
| FR-VAT-009 | Private use corrections (privégebruik) for vehicles, utilities and mixed-use assets in the final period of the year. | S |
| FR-VAT-010 | Deferred import VAT (artikel 23 vergunning) handling. | C |
| FR-VAT-011 | VAT audit trail: every figure in a filed return is drillable to the underlying postings and documents, permanently. | M |
| FR-VAT-012 | Corporate income tax (VPB) and income tax profit declaration pre-fill via the RGS brugstaat standard interface to tax software. | S (P3) |

### 6.7 Expenses, mileage and assets — `FR-EXP`

| ID | Requirement | Pri |
|---|---|---|
| FR-EXP-001 | Receipt capture by **camera** (single tap from the home screen, multi-page, auto edge detection, deskew, glare and blur warning with retake prompt) and by **upload** (drag-and-drop or file picker, accepting JPEG, PNG, HEIC, PDF and multi-page PDF). Both paths land in the same place. | M (P0) |
| FR-EXP-001a | Batch capture: photograph or upload several receipts in one session, each becoming a separate expense, with a review list before posting. | M (P0) |
| FR-EXP-001b | The expense form asks for the minimum — date, supplier, gross amount, VAT rate, category — with VAT and net calculated automatically and the category defaulting from the user's history. Everything else is optional and hidden by default. | M (P0) |
| FR-EXP-001c | Fields are pre-filled on a best-effort basis in P0 and are always editable. Confidence-scored extraction with a review queue is P1 (see FR-AP-002); the product never blocks on extraction being available. | M (P0) |
| FR-EXP-001d | The image is retained as the source document under §6.10, linked bidirectionally to the resulting posting, viewable at full resolution and re-orientable. | M (P0) |
| FR-EXP-001e | Payment method is captured at entry — paid by business account, business card, or personally (reimbursable) — because it determines the posting and cannot be reliably inferred later. | M (P0) |
| FR-EXP-001f | Capture works with no connectivity: the image is queued encrypted on-device and uploads automatically, with visible queue state (see MOB-003). | M |
| FR-EXP-001g | Duplicate detection warns when a receipt matching an existing expense on supplier, date and amount is captured. | M (P0) |
| FR-EXP-001h | Receipts can also arrive by forwarding an email to the administration's dedicated address, and by sharing to the app from another app (iOS share sheet, Android share intent). | S |
| FR-EXP-002 | Expense approval workflow separate from purchase invoice approval, with policy limits per category. | M |
| FR-EXP-003 | Reimbursement batch producing a SEPA payment file and the matching ledger entries. | M |
| FR-EXP-004 | Mileage registration with start/end address, distance, purpose, and the applicable untaxed per-kilometre allowance. | S |
| FR-EXP-005 | Fixed asset register: acquisition, useful life, residual value, depreciation method (straight line, declining balance), and monthly depreciation posting. | M |
| FR-EXP-006 | Asset disposal with gain/loss calculation and VAT revision (herziening) where applicable. | M |
| FR-EXP-007 | Investment deduction (KIA/EIA/MIA) flagging and calculation support. | C |

### 6.8 Reporting and insight — `FR-RPT`

| ID | Requirement | Pri |
|---|---|---|
| FR-RPT-001 | Standard reports: trial balance, general ledger detail, profit and loss, balance sheet, VAT summary, aged AR/AP, cash flow. | M |
| FR-RPT-002 | Every report drills down to transaction level and then to the source document image. | M |
| FR-RPT-003 | Period comparison: month, quarter, year, and against budget. | M |
| FR-RPT-004 | Owner dashboard: cash position, runway, revenue trend, outstanding receivables, VAT set-aside estimate, and items needing attention. | M |
| FR-RPT-005 | Export to XLSX, CSV and PDF; all exports are logged as data-access events. | M |
| FR-RPT-006 | RGS brugstaat export for onward reporting to tax and reporting software. | M |
| FR-RPT-007 | XAF 3.2 audit file export including RGS codes, for Belastingdienst inspection. | M |
| FR-RPT-008 | Annual accounts (jaarrekening) generation and SBR deposit to KvK for micro and small entities. | S (P3) |
| FR-RPT-009 | Budget entry per ledger account per period, with variance reporting. | S |
| FR-RPT-010 | Scheduled report delivery by email or to a shared drive. | C |

### 6.9 Accountant / firm portal — `FR-FRM`

| ID | Requirement | Pri |
|---|---|---|
Foundational firm capability ships in **P0** alongside the ledger, because the account model is not something that can be layered on later without reworking tenancy and IAM.

| ID | Requirement | Pri |
|---|---|---|
| FR-FRM-000 | Client switcher: searchable by client name, KvK number or trade name, keyboard-reachable, showing only granted administrations. Switching preserves the current screen type where it exists for the target client. | M (P0) |
| FR-FRM-000a | The active client is unmistakable at all times — persistent name and colour marker in the header on every screen, on web and mobile. Posting to the wrong client is the single worst usability failure in this product. | M (P0) |
| FR-FRM-000b | Client list supports grouping and tagging (by staff member, VAT frequency, deadline, status) and saved filters. | S |
| FR-FRM-001 | Portfolio dashboard listing all client administrations with status: documents pending, VAT deadline, unreconciled items, period lock state. | M (P1) |
| FR-FRM-002 | Cross-client work queue so staff work by task type rather than by opening each client in turn. | M |
| FR-FRM-003 | Client onboarding by the firm, including invitation of the client's own users with a scoped role. | M (P0) |
| FR-FRM-004 | Per-client access grant with explicit scope and optional expiry; a firm user sees only the administrations they are granted. | M (P0) |
| FR-FRM-004a | Client access profile management per client, per §8.6, with a preview showing exactly what the client will and will not see before the profile is applied. | M (P0) |
| FR-FRM-004b | Bulk assignment: apply a profile or a staff grant across a selected set of clients in one action, with a confirmation listing every affected client. | S |
| FR-FRM-005 | Question/answer threads attached to a specific transaction or document, resolvable, with client notification. | M |
| FR-FRM-006 | Year-end close checklist per client with sign-off steps and evidence attachment. | S |
| FR-FRM-007 | Firm-level branding on client-facing surfaces. | C |
| FR-FRM-008 | Client hand-back: full data export in open formats on termination, without support intervention. | M |

### 6.10 Documents and archive — `FR-DOC`

| ID | Requirement | Pri |
|---|---|---|
| FR-DOC-001 | Every document is stored in original form, unaltered, alongside any derived text and extracted fields. | M |
| FR-DOC-002 | Retention of 7 years from the end of the fiscal year (10 years where immovable property is involved), enforced by policy and not deletable by users. | M |
| FR-DOC-003 | Documents are linked to postings bidirectionally; a posting without a source document is flagged in a completeness report. | M |
| FR-DOC-004 | Full-text search across document content, supplier, amount and date. | M |
| FR-DOC-005 | Storage is write-once for the retention period; deletion before expiry requires a documented legal basis and privileged approval. | M |
| FR-DOC-006 | Bulk export of the complete archive with a manifest and checksums. | M |

### 6.11 Notifications — `FR-NTF`

| ID | Requirement | Pri |
|---|---|---|
| FR-NTF-001 | Event-driven notifications: approval requested, invoice paid, VAT deadline approaching, bank consent expiring, document failed to process, unusual login. | M |
| FR-NTF-002 | Per-user channel preferences across email, push and in-app, with a digest option. | M |
| FR-NTF-003 | Security-relevant notifications (new device login, permission granted, MFA change, data export) cannot be disabled. | M |
| FR-NTF-004 | Notifications never contain financial amounts or document contents in the push payload. | M |

---

## 7. Experience and platform requirements

One API serves all clients. No client holds business logic that determines an accounting outcome.

### 7.1 Design principles

**Easy to use is the moto.** It is treated here as a requirement with acceptance criteria, because principles without measurement get traded away under delivery pressure.

| # | Principle | What it means in practice |
|---|---|---|
| D1 | **One obvious next action per screen** | Every screen has a single primary action, visually dominant. Secondary actions are present but quiet. No screen presents five equally weighted buttons. |
| D2 | **Accounting vocabulary is optional** | A ZZP never has to know what a journal, a credit, or a control account is. Plain language on the surface — "money in", "money out", "waiting for approval" — with the accounting term available on demand for those who want it. |
| D3 | **The system proposes, the user confirms** | Coding, matching and categorisation arrive pre-filled with a visible confidence. The user's job is to agree or correct, not to construct from scratch. |
| D4 | **Progressive disclosure** | Advanced controls (cost centres, VAT overrides, manual journals) are hidden until relevant or until the user's role implies they need them. Complexity is available, never imposed. |
| D5 | **No dead ends** | Every error message states what happened, why, and the specific next action. "Validation failed" is not an acceptable string. |
| D6 | **Never lose work** | Drafts autosave. Navigation away warns. Connectivity loss queues rather than discards. A half-finished invoice survives a closed laptop. |
| D7 | **Reversibility over confirmation dialogues** | Prefer undo to "are you sure?". Where an action is genuinely irreversible (filing a return, sending an invoice, releasing a payment), say so explicitly and make it the only modal in the flow. |
| D8 | **Two audiences, one product** | The bookkeeper's keyboard-driven speed and the owner's occasional-use clarity are both served, by the same screens where possible and by role-aware defaults where not. |
| D9 | **Show the source** | Any number can be tapped through to the document behind it. Trust in a bookkeeping product comes from being able to check. |
| D10 | **Silence is a feature** | Notifications, badges and prompts are earned. An interface that constantly asks for attention gets ignored. |

### 7.2 Usability requirements

| ID | Requirement | Pri |
|---|---|---|
| FR-UX-001 | A new ZZP user, unassisted and without documentation, completes signup, connects a bank account, captures a receipt and sends an invoice. Measured by moderated usability testing with at least 8 representative participants; target ≥ 80% task completion before GA. | M |
| FR-UX-002 | Core tasks are reachable within 3 interactions from the home screen: capture a document, create an invoice, approve an item, see cash position, check what needs attention. | M |
| FR-UX-003 | No feature required for the P1 core loop depends on a settings change. Defaults are correct for the common case. | M |
| FR-UX-004 | Empty states teach: every list, before it holds data, explains what belongs there and offers the action that creates the first item. | M |
| FR-UX-005 | The home screen is a prioritised list of what needs the user's attention, not a menu of everything the product can do. | M |
| FR-UX-006 | Onboarding requires only what is needed to start. Anything deferrable (chart of accounts detail, VAT settings, branding) is asked for when it first matters. | M |
| FR-UX-007 | Error and validation messages are written in the user's language by a person, reviewed as content, and pass the D5 test. Untranslated or developer-facing strings never reach a user. | M |
| FR-UX-008 | A single design system shared across web, iOS and Android, with platform-appropriate navigation patterns rather than a lowest-common-denominator UI. | M |
| FR-UX-009 | Usability testing is run on every major flow before it ships, and a per-flow drop-off metric is instrumented afterwards. | M |
| FR-UX-010 | In-product guidance is contextual and dismissible; there are no forced multi-step product tours. | M |
| FR-UX-011 | Task-level telemetry tracks time-to-complete and abandonment for the flows in FR-UX-002; a regression is treated as a defect. | M |

### 7.3 Web application

| ID | Requirement | Pri |
|---|---|---|
| FR-WEB-001 | Responsive single-page application supporting the current and previous two major versions of Chrome, Edge, Safari and Firefox. | M |
| FR-WEB-002 | Keyboard-first entry paths for bookkeepers: tab order, shortcut keys, type-ahead account and VAT code selection, no mouse required for the core posting loop. | M |
| FR-WEB-003 | Bulk operations on lists: select, categorise, approve, export. | M |
| FR-WEB-004 | Split-screen document viewer with the coding form, side by side. | M |
| FR-WEB-005 | Optimistic UI with conflict resolution on concurrent edits to the same record. | S |
| FR-WEB-006 | No financial data persisted in browser local storage; session state is memory-resident and cleared on logout. | M |

### 7.4 Mobile applications (iOS and Android)

Mobile is deliberately not a full clone of the web app. It covers capture, approve, view and invoice — the tasks people do away from a desk.

| ID | Requirement | Pri |
|---|---|---|
| MOB-001 | Native or near-native implementation (React Native with native modules, or fully native) delivering 60fps scrolling and platform-standard navigation. | M |
| MOB-002 | Receipt and invoice capture: multi-page, auto edge detection, deskew, glare warning, and upload retry. Available in P0 via installable PWA; native camera path in P2. | M (P0) |
| MOB-003 | Offline capture queue — documents taken without connectivity are stored encrypted on-device and uploaded when connectivity returns, with visible queue state. | M |
| MOB-004 | Approve or reject purchase invoices and expenses, with the document viewable at full resolution. | M |
| MOB-005 | Create and send a sales invoice from the phone, using the administration's template. Template editing is web-only; the phone renders and sends. Peppol delivery from P2. | M |
| MOB-006 | Dashboard: cash position, receivables, VAT estimate, items needing attention. | M |
| MOB-007 | Push notifications via APNs and FCM, with payloads containing no financial content. | M |
| MOB-008 | Biometric unlock (Face ID / Touch ID / Android BiometricPrompt) backed by the platform keystore, with a PIN fallback and a configurable auto-lock timeout. | M |
| MOB-009 | No financial data written to unencrypted device storage; on-device cache is encrypted, size-capped and purged on logout, role change or remote wipe. | M |
| MOB-010 | Certificate pinning against the API domain, with a documented pin rotation procedure and a kill switch to avoid bricking clients. | M |
| MOB-011 | Jailbreak/root detection with a policy response (warn by default, block if tenant policy requires). | S |
| MOB-012 | Screenshot and screen-recording suppression on screens showing financial data, where the platform supports it. | S |
| MOB-013 | Accessibility: VoiceOver and TalkBack labels, Dynamic Type, minimum 44pt/48dp touch targets, sufficient contrast. | M |
| MOB-014 | Forced-upgrade mechanism when a client version is below the minimum supported API contract, or has a known security defect. | M |
| MOB-015 | Deep links from notifications and email into the specific record, with authentication enforced before content renders. | M |
| MOB-016 | Localisation: Dutch and English at GA, with the app language independent of device language if the user prefers. | M |

### 7.5 Cross-platform consistency

| ID | Requirement | Pri |
|---|---|---|
| FR-XPL-001 | A change made on any client is visible on all other active sessions within 5 seconds. | M |
| FR-XPL-002 | Permission changes take effect on all clients within 60 seconds without requiring the user to sign out. | M |
| FR-XPL-003 | API is versioned; a released mobile version remains supported for at least 12 months after its successor ships. | M |
| FR-XPL-004 | Feature flags are server-controlled so behaviour can be changed without an app store release cycle. | M |

---

## 8. Identity and access management

This section is a hard requirement set, not guidance. The design principle is **default deny with explicit, scoped, expiring grants**.

### 8.1 Tenancy model

```
Organization  (accounting firm, or a standalone business)
  └── Administration  (one legal entity's books; a tenant may hold many)
        └── Fiscal Year
              └── Period
```

| ID | Requirement | Pri |
|---|---|---|
| IAM-001 | Every data record carries an `organization_id` and, where applicable, an `administration_id`. No query path exists that can return records without a tenant predicate. | M |
| IAM-002 | Tenant isolation is enforced at the data layer (PostgreSQL row-level security or equivalent), not only in application code. Application-layer checks are the second line, not the first. | M |
| IAM-003 | Cross-tenant access is impossible by design; a firm's access to a client administration is an explicit grant relationship, not shared storage. | M |
| IAM-004 | A per-tenant encryption key is used for document and attachment storage, so a key revocation isolates one tenant. | M |
| IAM-005 | Automated tests assert tenant isolation on every endpoint; a new endpoint cannot ship without an isolation test. | M |

### 8.2 Authentication

| ID | Requirement | Pri |
|---|---|---|
| IAM-010 | Supported sign-in methods, offered side by side on one screen: **Continue with Google**, **passkey**, and **email + password**. No method is buried behind a "more options" link. | M |
| IAM-010a | Google sign-in uses OpenID Connect with PKCE, requesting only `openid`, `email` and `profile`. No Gmail, Drive, Calendar or Contacts scopes are requested at any point. | M |
| IAM-010b | The email address from Google must be verified (`email_verified: true`) before an account is created or matched. Unverified Google identities are rejected. | M |
| IAM-010c | Account linking: if a Google sign-in matches an existing verified email, the user is asked to authenticate with their existing method once before the identities are linked. Automatic linking on email alone is prohibited — it is an account takeover path. | M |
| IAM-010d | A user may hold multiple sign-in methods on one account and manage them in settings, provided at least one remains. Removing a method requires re-authentication. | M |
| IAM-010e | Google sign-in does not satisfy the MFA requirement on its own. Where the user's Google account has 2FA enabled and this is asserted in the token (`amr`), it may be accepted as the second factor; otherwise LEDGR enrols its own. | M |
| IAM-010f | Loss of the Google account must not lock a user out of their books: any user whose only method is Google is prompted to add a passkey or password before they can post to the ledger. | M |
| IAM-010g | Language is selectable **before** authentication, on the login and signup screens, defaulting to the browser or device locale and falling back to Dutch for `.nl` traffic. The choice persists on the device and is applied to the account after first login. | M |
| IAM-011 | MFA is mandatory for all users with any write permission, and for all users of any administration that has filed a tax return. No opt-out. | M |
| IAM-012 | Supported second factors: passkey, TOTP, platform biometrics bound to a device key. SMS is not offered. | M |
| IAM-013 | Passwords follow NIST SP 800-63B: minimum 12 characters, breached-password screening, no forced rotation, no composition rules. | M |
| IAM-014 | Enterprise SSO via SAML 2.0 and OIDC, with SCIM 2.0 user provisioning and deprovisioning for accounting firms. | S (P3) |
| IAM-015 | When SSO is enabled for an organization, local password login is disabled for its members except for a documented break-glass account. | M |
| IAM-016 | Sessions: 12-hour maximum lifetime, 30-minute idle timeout for privileged roles, absolute re-authentication for sensitive actions (payment initiation, permission change, data export, tax filing). | M |
| IAM-017 | Device sessions are listed to the user with location and last-use, and individually revocable. | M |
| IAM-018 | Account recovery never grants access on the strength of an email alone; recovery requires a second verified factor or an admin-initiated, logged reset. | M |
| IAM-019 | Rate limiting and progressive lockout on authentication endpoints; credential-stuffing detection with anomaly alerting. | M |
| IAM-020 | eHerkenning support where required for filings on behalf of a legal entity. | S |

### 8.3 Authorization model

Role-based access control provides the baseline; attribute-based rules narrow it further.

| ID | Requirement | Pri |
|---|---|---|
| IAM-030 | Permissions are expressed as `(action, resource_type, scope)` triples. Roles are named bundles of permissions. Users hold role assignments scoped to an organization or a specific administration. | M |
| IAM-031 | Default deny. Absence of a matching grant is a denial; there is no implicit inheritance that widens access. | M |
| IAM-032 | Scope narrowing only: a grant at administration level cannot be widened to organization level by any code path. | M |
| IAM-033 | Attribute conditions supplement roles: amount ceilings, cost centre restriction, journal restriction, period restriction, IP allowlist, device trust. | M |
| IAM-034 | Every authorization decision is evaluated server-side per request against current state. Client-side hiding of UI is presentation only and never the control. | M |
| IAM-035 | Grants may carry an expiry timestamp. Expired grants stop working without any administrative action. | M |
| IAM-036 | Custom roles can be composed by organization admins, but only from permissions they themselves hold — no privilege escalation by role authoring. | M |
| IAM-037 | Permission changes take effect within 60 seconds across all active sessions; token claims are short-lived and re-validated. | M |

### 8.4 Standard roles

| Role | Scope | Core capability | Explicitly cannot |
|---|---|---|---|
| **Owner** | Organization | Everything, including deleting the organization | Bypass audit logging; view another tenant |
| **Organization Admin** | Organization | User and role management, administration creation, settings | Post journals, initiate payments, approve their own grants |
| **Security Admin** | Organization | MFA policy, session policy, IP allowlists, access reviews, audit log read | Read financial data; grant themselves financial roles |
| **Billing Admin** | Organization | Subscription, invoices, payment method for the LEDGR subscription | Access administrations' financial data |
| **Firm Manager** | Firm organization | Client portfolio management, client onboarding, assigning firm staff to clients, setting client access profiles | Post to a client's ledger without also holding Accountant on it; remove a client's rights floor (IAM-105) |
| **Accountant** | Per administration | Full bookkeeping, period lock/unlock, close, filing | Change organization user roles; alter audit log |
| **Bookkeeper** | Per administration | Post to permitted journals, reconcile bank, prepare returns | Unlock closed periods; approve payments; file returns |
| **Approver** | Per administration, optional cost centre | Approve/reject invoices and expenses within an amount ceiling | Create the invoices they approve; post to the ledger |
| **Invoicer** | Per administration | Create and send sales invoices, manage customers | See purchase invoices, bank data, payroll or reports |
| **Expense Submitter** | Per administration | Submit own expenses and mileage, view own history | See any data belonging to another person |
| **Viewer / Auditor** | Per administration, optionally per fiscal year | Read-only access to ledger, documents and reports; export | Write anything; see user credentials or security settings |
| **Service Account** | Per administration, per integration | Scoped API access to named endpoints | Interactive login; broaden its own scope |

### 8.5 Least privilege enforcement

| ID | Requirement | Pri |
|---|---|---|
| IAM-050 | New users are created with no administration access. Access is granted per administration, deliberately. | M |
| IAM-051 | The invitation flow requires the inviter to choose a role and scope before the invitation can be sent; there is no "decide later" default that grants access. | M |
| IAM-052 | Firm access to a client administration requires the client's own acceptance, is scoped, and is revocable by the client at any time without contacting the firm. | M |
| IAM-053 | Just-in-time elevation: temporary elevation to a higher role requires a stated reason, an approver, a maximum duration of 8 hours, and automatic expiry. | M |
| IAM-054 | Break-glass accounts exist for organization lockout recovery. They are MFA-protected, unused in normal operation, and every use raises an immediate alert to the Owner and to LEDGR security. | M |
| IAM-055 | Access recertification: every organization with more than 5 users receives a quarterly review listing users, roles, scopes, last activity and dormancy. Unreviewed grants older than two cycles are flagged prominently. | M |
| IAM-056 | Dormant accounts (no login for 90 days) are automatically suspended, requiring admin reactivation. | M |
| IAM-057 | Offboarding: deactivating a user immediately terminates sessions, revokes tokens, disables API keys they own, and reassigns their pending approvals. | M |
| IAM-058 | Permission usage analytics show admins which granted permissions a user has never exercised, to support right-sizing. | S |

### 8.6 Firm control of client access

In Model A the firm decides what the client's own users can do inside their administration. This is delegated administration with a hard floor: there are client rights the firm cannot remove.

| ID | Requirement | Pri |
|---|---|---|
| IAM-100 | For each managed administration the firm selects a **client access profile** that governs what the client's own users may do. Profiles are firm-defined, reusable across clients, and versioned. | M |
| IAM-101 | Three built-in profiles ship as defaults, and a firm may compose its own: **Capture only** (upload documents, submit expenses, view own submissions), **Invoice and capture** (adds sales invoicing, customers, and viewing own reports), **Full self-service** (adds coding, bank reconciliation and VAT preparation, while the firm retains filing and period control). | M |
| IAM-102 | A firm cannot grant a client's users a permission the firm itself does not hold on that administration. | M |
| IAM-103 | Profile changes take effect within 60 seconds, are logged, and the client's Owner is notified with a plain-language summary of what changed. Silent reduction of a client's access is prohibited. | M |
| IAM-104 | The firm may restrict, per profile: which journals are visible, whether bank transaction detail is visible, whether reports are visible, whether periods can be edited, and amount ceilings on approvals. | M |
| IAM-105 | **Client rights floor.** Regardless of profile, the client's Owner always retains: read access to their own source documents and filed returns, export of their complete data, visibility of their own audit log, the ability to revoke the firm's access, and the ability to manage their own users' sign-in security. No firm setting can remove these. | M |
| IAM-106 | The client's Owner manages their own users within the ceiling the profile allows. The firm does not administer the client's staff accounts unless the client explicitly delegates that. | M |
| IAM-107 | Firm staff access is granted per client administration, not per firm. A new firm employee starts with access to zero clients. | M |
| IAM-108 | Firm staff grants may carry an expiry, and support seasonal or interim staff working on a defined client set for a defined period. | M |
| IAM-109 | The client sees, at any time and without asking, which firm users hold access to their administration, with what role, since when, and when each last accessed it. | M |
| IAM-110 | On revocation of firm access, firm sessions for that administration terminate immediately, the administration disappears from the firm switcher, and the client retains all data including entries the firm made. | M |
| IAM-111 | Client access profiles, and every change to them, appear in both the firm's and the client's audit log. | M |

### 8.7 Segregation of duties

| ID | Requirement | Pri |
|---|---|---|
| IAM-060 | The user who creates or edits a purchase invoice cannot be the sole approver of it. | M |
| IAM-061 | The user who approves a payment batch cannot be the user who releases it to the bank, where the organization has more than two active users. | M |
| IAM-062 | The user who submits an expense cannot approve it. | M |
| IAM-063 | A user cannot grant themselves a permission they do not hold, nor approve their own access request. | M |
| IAM-064 | SoD rules are configurable per organization but each deviation must be explicitly acknowledged by the Owner, is recorded with a reason, and appears in the audit report. | M |
| IAM-065 | Single-user organizations are exempt from SoD enforcement by necessity, and this exemption is disclosed in the audit report rather than hidden. | M |

### 8.8 Internal (LEDGR staff) access

| ID | Requirement | Pri |
|---|---|---|
| IAM-070 | No LEDGR employee has standing access to customer financial data. None. | M |
| IAM-071 | Support access requires: an open support case, a stated reason, approval by a second employee, a maximum 4-hour window, and automatic expiry. | M |
| IAM-072 | Support access is visible to the customer in their own audit log, in real time, including who accessed what and why. | M |
| IAM-073 | Customers may require explicit in-app consent before any support access is possible; this is a tenant setting, default on for organizations above a defined size. | M |
| IAM-074 | Production database access by engineers is read-only by default, brokered through a bastion with session recording, and write access requires change-approval reference. | M |
| IAM-075 | Support tooling shows masked data by default; unmasking is a separate, individually logged action. | M |
| IAM-076 | All internal access events feed the SIEM and are reviewed weekly; anomalies trigger investigation within one business day. | M |

### 8.9 API and machine identity

| ID | Requirement | Pri |
|---|---|---|
| IAM-080 | Public API uses OAuth 2.1 with PKCE for user-delegated access and client credentials for server-to-server, both with narrowly scoped tokens. | M |
| IAM-081 | Scopes are granular per resource and per operation (e.g. `invoices:read`, `invoices:write`, `bank:read`); there is no all-access scope. | M |
| IAM-082 | Access tokens are short-lived (≤ 15 minutes); refresh tokens rotate on use with reuse detection that revokes the family. | M |
| IAM-083 | Users see every connected application, the scopes it holds, its last use, and can revoke it in one action. | M |
| IAM-084 | API keys, where used for simple integrations, are shown once, stored hashed, scoped, expiring, and rotatable without downtime. | M |
| IAM-085 | Per-client rate limits and quotas, with 429 responses carrying retry guidance. | M |
| IAM-086 | Webhook payloads are signed; consumers can verify authenticity and replay-protect via timestamp. | M |

### 8.10 Audit logging

| ID | Requirement | Pri |
|---|---|---|
| IAM-090 | Append-only audit log covering: authentication events, permission grants and revocations, data reads of financial records, exports, postings, approvals, filings, configuration changes and support access. | M |
| IAM-091 | Each entry records actor, actor type, tenant, resource, action, outcome, timestamp (UTC), source IP, user agent and correlation ID. | M |
| IAM-092 | The audit log is immutable and tamper-evident (hash chaining or WORM storage). No role, including Owner or LEDGR staff, can edit or delete entries. | M |
| IAM-093 | Retention of audit logs for 7 years to match fiscal record retention. | M |
| IAM-094 | Customers can search and export their own audit log without contacting support. | M |
| IAM-095 | Security-relevant events stream to the SIEM in near real time with alerting rules. | M |

---

## 9. Security requirements

### 9.1 Threat model summary

| Threat | Primary controls |
|---|---|
| Account takeover | Passkeys, mandatory MFA, breached-credential screening, anomaly detection, device session management |
| Cross-tenant data leakage | Row-level security, per-tenant keys, mandatory isolation tests, no shared caches keyed without tenant |
| Insider access abuse | No standing access, JIT approval, session recording, customer-visible access log |
| Invoice fraud / IBAN swap | Supplier bank detail change alerts, SoD on payment release, out-of-band change verification |
| Malicious document upload | Sandboxed parsing, malware scanning, content-type verification, no server-side rendering of untrusted content |
| Supply chain compromise | SBOM, pinned dependencies, signed builds, provenance attestation |
| Data exfiltration via API | Scoped tokens, rate limits, export logging, egress anomaly detection |
| Ransomware | Immutable backups, isolated backup credentials, restore drills |

### 9.2 Application security

| ID | Requirement | Pri |
|---|---|---|
| SEC-001 | The application meets OWASP ASVS Level 2 in full; Level 3 for authentication, session management and access control. | M |
| SEC-002 | All input validated server-side against an explicit schema; parameterised queries only; output encoding by context. | M |
| SEC-003 | Content Security Policy with nonce-based script allowance, no `unsafe-inline`, no `unsafe-eval`. | M |
| SEC-004 | HSTS with preload, `SameSite=Strict` session cookies, `Secure` and `HttpOnly` flags, anti-CSRF tokens on state-changing requests. | M |
| SEC-005 | File uploads: type verified by content not extension, size-capped, malware-scanned, stored outside the web root, served from a separate origin with `Content-Disposition: attachment`. | M |
| SEC-006 | Document parsing and OCR run in an isolated, network-restricted sandbox with no credentials. | M |
| SEC-007 | Server-side request forgery defences on every outbound fetch (URL allowlists, no redirect following to internal ranges). | M |
| SEC-008 | Error responses never disclose stack traces, internal identifiers, or whether an account exists. | M |
| SEC-009 | Security headers, TLS configuration and cipher suites verified continuously by automated scanning. | M |

### 9.3 Cryptography and data protection

| ID | Requirement | Pri |
|---|---|---|
| SEC-020 | TLS 1.3 (1.2 minimum) for all external traffic; mutual TLS for service-to-service where feasible. | M |
| SEC-021 | AES-256 encryption at rest for databases, object storage, backups and message queues. | M |
| SEC-022 | Envelope encryption with a managed KMS/HSM; per-tenant data encryption keys wrapped by a key-encryption key. | M |
| SEC-023 | Automatic key rotation at least annually, with re-wrapping and no downtime; emergency rotation procedure documented and tested. | M |
| SEC-024 | Field-level encryption for bank account numbers, national identifiers and authentication secrets, distinct from storage-level encryption. | M |
| SEC-025 | Secrets are never in source control, environment variables in plain text, or logs. Managed secret store with short-lived dynamic credentials. | M |
| SEC-026 | Cryptographic agility: algorithms and key sizes are configuration, with a documented post-quantum migration position. | S |

### 9.4 Infrastructure and operations security

| ID | Requirement | Pri |
|---|---|---|
| SEC-030 | Infrastructure as code; no manual production changes. Every change goes through review, CI checks and an approved deployment. | M |
| SEC-031 | Network segmentation: public edge, application tier, data tier. Data tier has no route to the internet. | M |
| SEC-032 | WAF and DDoS protection at the edge; bot management on authentication and signup endpoints. | M |
| SEC-033 | Workload identity for service authentication; no long-lived static credentials between services. | M |
| SEC-034 | Container images built from minimal bases, scanned for vulnerabilities, signed, and admitted only if signature and policy checks pass. | M |
| SEC-035 | Patch SLA: critical vulnerabilities in internet-facing components within 24 hours, high within 7 days, medium within 30 days. | M |
| SEC-036 | Centralised logging with tamper protection; logs retained 13 months for security events, 7 years for audit events. | M |
| SEC-037 | SIEM with detection rules for credential stuffing, impossible travel, mass export, privilege escalation, and anomalous internal access. | M |
| SEC-038 | Backups encrypted, stored in a separate account/subscription with separate credentials, immutable for their retention window. | M |
| SEC-039 | Restore drills quarterly, with documented RTO/RPO achievement; a failed drill blocks the next release train. | M |

### 9.5 Secure development lifecycle

| ID | Requirement | Pri |
|---|---|---|
| SEC-050 | Threat modelling for every new subsystem and for any change to authentication, authorization or payment flows. | M |
| SEC-051 | SAST, dependency/SCA scanning and secret scanning in CI; builds fail on new critical findings. | M |
| SEC-052 | DAST against a production-like environment on every release candidate. | M |
| SEC-053 | Two-person review on all code; a separate reviewer group required for authentication, authorization and cryptography code paths. | M |
| SEC-054 | SBOM generated per release and retained; build provenance attested. | M |
| SEC-055 | Independent penetration test before GA and at least annually thereafter, plus after any material architecture change. Findings tracked to closure with owner and date. | M |
| SEC-056 | Public vulnerability disclosure policy and security.txt at launch; bug bounty programme within 6 months of GA. | M |
| SEC-057 | Separate development, test, staging and production environments. Production data is never copied to lower environments; test data is synthetic or irreversibly anonymised. | M |

### 9.6 Incident response and resilience

| ID | Requirement | Pri |
|---|---|---|
| SEC-060 | Documented incident response plan with severity definitions, on-call rotation, communication templates and decision authority. | M |
| SEC-061 | Personal data breaches notified to the Autoriteit Persoonsgegevens within 72 hours of becoming aware, and to affected controllers without undue delay. | M |
| SEC-062 | Incident response exercised at least twice a year, including one exercise involving a simulated tenant data exposure. | M |
| SEC-063 | Post-incident review within 5 working days, blameless, with tracked corrective actions. | M |
| SEC-064 | Status page with honest, timely incident communication; no silent degradation. | M |

### 9.7 Certification and assurance

| ID | Requirement | Pri |
|---|---|---|
| SEC-070 | ISO/IEC 27001 certification, targeted within 12 months of GA. | M |
| SEC-071 | SOC 2 Type II report, targeted within 18 months of GA. | S |
| SEC-072 | Trust centre publishing sub-processors, certifications, uptime, and the security whitepaper. | M |
| SEC-073 | Vendor risk assessment for every sub-processor handling customer data, reviewed annually. | M |

---

## 10. Privacy and data protection

### 10.1 Roles and lawful basis

LEDGR is a **controller** for account, billing and product-usage data about its own users. LEDGR is a **processor** for the financial and personal data inside customer administrations — the customer, or the accounting firm, is the controller there. The distinction must be reflected in contracts, in the product's own language, and in how deletion and export requests are handled.

| ID | Requirement | Pri |
|---|---|---|
| PRIV-001 | A Data Processing Agreement including EU Standard Contractual Clauses is part of the standard terms, accepted at signup, versioned and retrievable by the customer. | M |
| PRIV-002 | Lawful basis is documented per processing purpose in a Record of Processing Activities, maintained and reviewed annually. | M |
| PRIV-003 | A Data Protection Impact Assessment is completed before GA, covering automated document processing, bank data access and profiling, and is reviewed on material change. | M |
| PRIV-004 | A Data Protection Officer or equivalent accountable role is appointed and contactable. | M |

### 10.2 Data minimisation and residency

| ID | Requirement | Pri |
|---|---|---|
| PRIV-010 | All customer data is stored and processed within the EU. The primary region is the Netherlands, with EU-only failover. | M |
| PRIV-011 | No sub-processor may store or access customer data outside the EU. Any exception requires DPO approval, a documented transfer mechanism and customer notification. | M |
| PRIV-012 | Collect only what a bookkeeping function requires. No contact-list harvesting, no device identifiers beyond what push notification requires, no advertising identifiers, no third-party trackers in the apps. | M |
| PRIV-013 | Bank access is read-only account information, scoped to accounts the user selects, with consent renewal prompts before expiry and one-tap revocation. | M |
| PRIV-014 | Analytics are event-based and pseudonymised; no financial amounts, counterparty names or document contents leave the production boundary in telemetry. | M |
| PRIV-015 | Customer data is not used to train shared machine learning models unless the customer opts in explicitly, per organization, with the option revocable and the default off. Per-tenant learning from a tenant's own corrections is always confined to that tenant. | M |
| PRIV-016 | Sub-processor list is public, and customers are notified at least 30 days before a new sub-processor is added, with a right to object. | M |

### 10.3 Data subject rights

| ID | Requirement | Pri |
|---|---|---|
| PRIV-020 | Self-service export of all data associated with a user account, in machine-readable format, without contacting support. | M |
| PRIV-021 | Controller-initiated data subject requests (access, rectification, erasure, portability, restriction) are supported through tooling that completes within the statutory one-month window. | M |
| PRIV-022 | Erasure requests are honoured **except** where fiscal retention law requires preservation. The system distinguishes the two and gives a clear, specific explanation of what was retained and why. | M |
| PRIV-023 | Where erasure is blocked by retention law, the record is restricted: access limited to fiscal purposes, excluded from analytics and search, deleted automatically at the end of the retention period. | M |
| PRIV-024 | Personal data in free-text fields is included in export and erasure scope; the system does not treat notes and comments as out of reach. | M |

### 10.4 Retention

| Data category | Retention | Basis |
|---|---|---|
| Ledger postings, invoices, source documents | 7 years from end of fiscal year (10 years for immovable property) | Dutch fiscal law |
| Audit log | 7 years | Fiscal traceability |
| Bank transaction data | 7 years | Fiscal law |
| PSD2 consent records | Duration of consent + 5 years | PSD2 / dispute evidence |
| Account and profile data | Duration of contract + 90 days | Contract |
| Billing records | 7 years | Fiscal law |
| Security logs | 13 months | Security operations |
| Support conversations | 24 months | Contract / service |
| Product analytics (pseudonymised) | 25 months | Legitimate interest |
| Deleted-tenant backups | Purged within 90 days of contract end | Data minimisation |

| ID | Requirement | Pri |
|---|---|---|
| PRIV-030 | Retention is enforced by automated jobs, not manual process, with an exception report for records that failed to expire. | M |
| PRIV-031 | On contract termination, the customer has 90 days of read-only export access, after which data is deleted from live systems and purged from backups within a further 90 days. A deletion certificate is issued. | M |
| PRIV-032 | Legal hold capability suspends deletion for identified records, with the hold itself logged and justified. | M |

### 10.5 Transparency

| ID | Requirement | Pri |
|---|---|---|
| PRIV-040 | Plain-language privacy notice, in Dutch and English, describing what is collected, why, on what basis, with whom it is shared and for how long. | M |
| PRIV-041 | Consent for optional processing (marketing, opt-in model training, non-essential analytics) is granular, separately revocable, and never bundled into terms acceptance. | M |
| PRIV-042 | Cookie consent compliant with the Telecommunicatiewet and GDPR, with rejection as easy as acceptance and no non-essential cookies before consent. | M |
| PRIV-043 | Where an automated decision materially affects a user (e.g. auto-posting, fraud flagging), the reasoning is explainable and human review is available. | M |

---

## 11. Regulatory and fiscal compliance

| ID | Requirement | Pri |
|---|---|---|
| CMP-001 | Administratieplicht: complete, accurate records retained 7 years, retrievable within a reasonable period, in a form an inspector can audit. | M |
| CMP-002 | XAF 3.2 audit file export including RGS codes, validating against the official schema. | M |
| CMP-003 | RGS 3.8 (and successors) reference codes maintained on the chart of accounts, with a versioned upgrade path when a new RGS version is released. | M |
| CMP-004 | SBR filings via Digipoort using the current Nederlandse Taxonomie; the platform tracks Logius interface deprecations and migrates ahead of enforced cut-off dates. | M |
| CMP-005 | Invoices meet Dutch statutory content requirements; the system refuses to issue a non-compliant invoice. | M |
| CMP-006 | E-invoicing: EN 16931 compliant, Peppol BIS Billing 3.0 and NLCIUS, exchanged over the Peppol four-corner network via a certified Access Point. | M |
| CMP-007 | B2G e-invoicing to Dutch central and sub-central government is supported at GA. | M |
| CMP-008 | ViDA readiness: the invoice data model is structured-first, with an architectural seam for a digital reporting layer ahead of the July 2030 intra-Community deadline and any domestic Dutch mandate that follows. This is a design constraint from day one, not a later project. | M |
| CMP-009 | Ledger immutability and gapless numbering sufficient to satisfy inspection standards; deletion of posted entries is impossible through any interface, including support tooling. | M |
| CMP-010 | PSD2 account access is performed through a licensed AISP; LEDGR does not itself hold a licence and does not perform payment initiation without a licensed PISP partner. | M |
| CMP-011 | Wwft obligations are assessed; where LEDGR's activities fall outside scope this is documented, and customer-facing features do not imply LEDGR performs client due diligence on the customer's behalf. | M |
| CMP-012 | Accessibility conformance with WCAG 2.2 Level AA, aligned to EN 301 549 and the European Accessibility Act. | M |
| CMP-013 | A regulatory watch function tracks changes to VAT rates, rubrieken, taxonomy versions, RGS versions and e-invoicing mandates, with a defined lead time for product changes. | M |
| CMP-014 | Rate and rule changes are effective-dated, so historical periods keep the rules that applied at the time. | M |

---

## 12. Non-functional requirements

### 12.1 Performance

| ID | Requirement | Target |
|---|---|---|
| NFR-001 | API response time, read endpoints | p95 < 300 ms, p99 < 800 ms |
| NFR-002 | API response time, write endpoints | p95 < 600 ms |
| NFR-003 | Web app first contentful paint on a standard connection | < 1.5 s |
| NFR-004 | Mobile app cold start to usable | < 2.0 s |
| NFR-005 | Document upload to extracted-and-proposed | p95 < 30 s |
| NFR-006 | Report generation, 12 months of data for a typical SMB | < 5 s |
| NFR-007 | Bank feed sync latency after bank posting | < 4 hours |
| NFR-008 | Search across a 7-year document archive | < 2 s |

### 12.2 Scale

| ID | Requirement | Target |
|---|---|---|
| NFR-010 | Tenants | 100,000 administrations |
| NFR-011 | Transactions per administration per year | 250,000 without degradation |
| NFR-012 | Concurrent active users | 20,000 |
| NFR-013 | Document ingestion peak | 500 documents/second (VAT deadline weeks drive 10x normal load) |
| NFR-014 | Capacity planning explicitly models quarter-end and VAT-deadline spikes, not average load | — |

### 12.3 Availability and continuity

| ID | Requirement | Target |
|---|---|---|
| NFR-020 | Availability SLA | 99.9% monthly, excluding announced maintenance |
| NFR-021 | Planned maintenance | Outside business hours, announced 5 days ahead, never in the 5 days before a VAT deadline |
| NFR-022 | RPO | 15 minutes |
| NFR-023 | RTO | 4 hours |
| NFR-024 | Multi-AZ deployment with automated failover | Required |
| NFR-025 | Cross-region EU disaster recovery, tested annually | Required |
| NFR-026 | Graceful degradation: if OCR, bank feeds or Peppol are unavailable, core bookkeeping continues and queued work resumes automatically | Required |

### 12.4 Data integrity

| ID | Requirement | Pri |
|---|---|---|
| NFR-030 | All financial mutations are transactional; partial writes are impossible. | M |
| NFR-031 | Monetary values use decimal types with defined scale. Floating point is prohibited in any financial calculation path. | M |
| NFR-032 | Idempotency keys on all mutating API endpoints so retries cannot double-post. | M |
| NFR-033 | A nightly integrity job verifies debit/credit balance, sub-ledger to control account agreement, and numbering continuity, alerting on any deviation. | M |
| NFR-034 | Optimistic concurrency control with version stamps; conflicting edits are surfaced, never silently overwritten. | M |

### 12.5 Maintainability and observability

| ID | Requirement | Pri |
|---|---|---|
| NFR-040 | Structured logging with correlation IDs traceable across web, mobile, API and background jobs. | M |
| NFR-041 | Distributed tracing and RED/USE metrics on every service; SLOs defined with error budgets. | M |
| NFR-042 | Automated test coverage: unit, integration, contract and end-to-end. The accounting engine has property-based tests asserting invariants (balance, immutability, period integrity). | M |
| NFR-043 | Zero-downtime deployments with progressive rollout and automated rollback on SLO breach. | M |
| NFR-044 | Database migrations are backward compatible and reversible; no migration requires downtime. | M |
| NFR-045 | Runbooks for every alert; alerts that cannot be acted on are deleted, not tolerated. | M |

---

## 13. Architecture and technology

Proposed, not prescribed — but each choice below carries a reason tied to a requirement above.

| Layer | Choice | Rationale |
|---|---|---|
| Cloud | Azure, West Europe primary with EU failover | EU residency (PRIV-010); strong managed Postgres, KMS and identity services |
| API | Python/FastAPI or .NET, REST + OpenAPI, versioned | One contract for three clients (FR-XPL-003) |
| Core datastore | PostgreSQL with row-level security | Tenant isolation at the data layer (IAM-002) |
| Ledger storage | Append-only posting tables, no UPDATE or DELETE grants on committed rows | Immutability (FR-GL-003, CMP-009) |
| Document storage | Azure Blob with immutability policies and per-tenant keys | Retention (FR-DOC-005), key isolation (IAM-004) |
| Search | OpenSearch, tenant-partitioned indices | Archive search (NFR-008) |
| Async processing | Queue-based workers for OCR, matching, filing, notifications | Graceful degradation (NFR-026) |
| Document AI | Managed extraction service in-region, plus a tenant-scoped correction model | Extraction accuracy with privacy boundary (PRIV-015) |
| Analytics | Separate warehouse fed by change data capture; pseudonymised | No analytics queries on the transactional store (PRIV-014) |
| Web | React/TypeScript SPA | — |
| Mobile | React Native with native modules for camera, keystore, biometrics; or fully native if performance targets are missed | MOB-001, MOB-008 |
| Identity | Managed IdP for authentication; authorization logic owned in-house | Custom RBAC/ABAC model (IAM-030) |
| Secrets | Azure Key Vault / managed HSM | SEC-022, SEC-025 |

**Architectural non-negotiables**

1. The ledger is a separate bounded context with a narrow API. Nothing writes to posting tables except the ledger service.
2. Authorization is a single library used by every service; there is no second implementation.
3. Every request carries tenant context from edge to database. A request without tenant context fails closed.
4. Integrations (banks, Peppol, Digipoort, PSPs) sit behind adapters, so a provider can be replaced without touching domain logic.

---

## 14. Integrations and public API

| ID | Requirement | Pri |
|---|---|---|
| FR-API-001 | Public REST API covering all core resources, documented with OpenAPI, with a sandbox environment and test credentials. | M |
| FR-API-002 | Webhooks for the significant events (invoice paid, document processed, approval needed, filing accepted), signed and retried with exponential backoff. | M |
| FR-API-003 | Deprecation policy: 12 months' notice, no breaking changes within a major version. | M |
| FR-API-004 | Bulk import/export endpoints for migration and for accountant tooling. | M |
| FR-API-005 | Rate limits published, per-client and per-endpoint, with headers exposing remaining quota. | M |

**Integration surface at GA:** KvK register, VIES VAT validation, PSD2 aggregator, Peppol Access Point, Digipoort/SBR, PSP for iDEAL and cards, email delivery, payroll (Nmbrs, Loket), webshops (Shopify, WooCommerce), POS (phase 3), Google Drive/OneDrive document import.

---

## 15. Core data model

| Entity | Key attributes | Notes |
|---|---|---|
| Organization | id, name, type (firm / business), settings | Billing and IAM boundary |
| Administration | id, organization_id, legal entity details, KvK, VAT numbers, fiscal calendar | Books boundary |
| User | id, identity, MFA state, status | Global identity, tenant-scoped access |
| RoleAssignment | user_id, role, scope_type, scope_id, conditions, granted_by, expires_at | Core of IAM |
| Account | administration_id, code, name, type, rgs_code, vat_default, status | Chart of accounts |
| Journal | administration_id, type, numbering sequence | Posting container |
| Transaction | administration_id, journal_id, date, period, description, document_id, actor, created_at | Header |
| PostingLine | transaction_id, account_id, debit, credit, vat_code, cost_centre, counterparty | Immutable |
| Document | administration_id, original blob ref, hash, extraction result, retention_until | Write-once |
| Contact | administration_id, type (customer/supplier), identifiers, payment details | IBAN changes audited |
| SalesInvoice / PurchaseInvoice | contact, lines, VAT breakdown, status, delivery channel, peppol_id | Links to Transaction |
| BankAccount / BankTransaction | IBAN, consent ref, statement lines, match state | Consent lifecycle tracked |
| VatReturn | administration_id, period, rubriek values, status, filing receipt | Locks its period |
| AuditEvent | actor, tenant, resource, action, outcome, timestamp, hash_prev | Append-only, hash-chained |

---

## 16. Commercial requirements

| ID | Requirement | Pri |
|---|---|---|
| FR-BIL-001 | Tiered subscription by administration, with feature gating and transaction volume bands. | M |
| FR-BIL-002 | Firm licensing: per-client pricing with volume tiers, billed to the firm. | M |
| FR-BIL-003 | Self-service signup, trial, upgrade, downgrade and cancellation without contacting sales. | M |
| FR-BIL-004 | Payment by SEPA direct debit, iDEAL and card; dunning on failed payment with a grace period before restriction. | M |
| FR-BIL-005 | On non-payment, the account becomes read-only with export available. Customer data is never deleted or withheld as leverage over an unpaid invoice, within statutory retention. | M |
| FR-BIL-006 | In-app purchase rules: subscriptions sold outside the apps where store policy permits; the apps must not break Apple or Google billing rules. Verify current policy before submission. | M |

---

## 17. Analytics and telemetry

| ID | Requirement | Pri |
|---|---|---|
| FR-ANL-001 | Product analytics events for the funnels behind the success metrics in §3.2, pseudonymised and EU-hosted. | M |
| FR-ANL-002 | Extraction and matching quality tracked as first-class metrics: confidence distribution, correction rate, reversal rate. | M |
| FR-ANL-003 | No session recording or heatmapping on any screen displaying financial data. | M |
| FR-ANL-004 | Telemetry opt-out available; opting out never degrades functionality. | M |

---

## 18. Support and operations

| ID | Requirement | Pri |
|---|---|---|
| FR-OPS-001 | In-app help, searchable knowledge base in Dutch and English, and contextual guidance at points of accounting judgement. | M |
| FR-OPS-002 | Support channels with published response targets; escalation path for filing-deadline emergencies. | M |
| FR-OPS-003 | Seasonal capacity plan for VAT deadline weeks across support and infrastructure. | M |
| FR-OPS-004 | Status page, incident history and maintenance calendar, public. | M |
| FR-OPS-005 | Customer-facing changelog; material changes to accounting behaviour announced in advance. | M |

---

## 19. App store requirements

| ID | Requirement | Pri |
|---|---|---|
| MOB-020 | Apple App Store and Google Play listings with accurate privacy labels and data safety declarations that match actual behaviour. | M |
| MOB-021 | Account deletion available in-app, as both stores require, honouring the retention rules in §10.4 with a clear explanation of what is retained and why. | M |
| MOB-022 | Demo account provided to app reviewers with representative data so review is not blocked by the login wall. | M |
| MOB-023 | Camera, notification and biometric permissions requested contextually with a clear purpose string, never at first launch. | M |
| MOB-024 | Age rating, export compliance and encryption declarations completed correctly. | M |
| MOB-025 | Staged rollout on Play; phased release on App Store; crash-rate thresholds trigger automatic halt. | M |
| MOB-026 | Minimum supported OS versions defined and reviewed annually; security patches shipped to supported versions within the SEC-035 SLA. | M |

---

## 20. Localisation and accessibility

| ID | Requirement | Pri |
|---|---|---|
| FR-LOC-001 | Dutch and English are both first-class from **P0** — not an English product with a Dutch translation added later. Every screen, error message, email, push notification, PDF template, help article and validation message exists in both. A missing translation is a release blocker, not a fallback to English. | M (P0) |
| FR-LOC-001a | Language is selectable before login (IAM-010g) and changeable at any time from the user menu in one click, taking effect immediately without reload or re-authentication. | M (P0) |
| FR-LOC-001b | Language is a per-user setting, not per-organization. A Dutch bookkeeper and an English-speaking owner can work in the same administration, each in their own language, seeing the same data. | M (P0) |
| FR-LOC-001c | Dutch accounting terminology is authoritative and reviewed by a practising Dutch accountant. Statutory terms keep their Dutch form in the English UI where no accurate equivalent exists (BTW, KvK, suppletie, grootboek), with a hover or tap definition. Machine translation of accounting terms is prohibited. | M |
| FR-LOC-001d | Translation completeness is enforced in CI: a build fails if any user-facing string lacks a translation in either language. | M |
| FR-LOC-002 | Locale-correct number, date and currency formatting; European decimal comma. Formatting follows the administration's locale, not the user's UI language, so amounts read identically to every user. | M |
| FR-LOC-003 | Invoice templates and email localised per recipient, independent of the sender's UI language. | M |
| FR-LOC-004 | WCAG 2.2 AA across web and mobile, verified by automated testing in CI and by manual audit before GA. | M |
| FR-LOC-005 | Architecture supports adding a jurisdiction (chart of accounts, VAT rules, filing channel, e-invoicing network) without forking the codebase. | M |

---

## 21. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Digipoort or taxonomy changes break filing near a deadline | Critical | Version-pinned integration, staging against test endpoints, regulatory watch with lead time, manual filing fallback |
| R2 | A tenant isolation defect exposes cross-customer data | Critical | RLS as primary control, mandatory isolation tests, per-tenant keys, external pentest focused on multi-tenancy |
| R3 | Extraction accuracy below expectation destroys the automation claim | High | Confidence thresholds default conservative, correction rate tracked, human-in-the-loop always available |
| R4 | Bank aggregator coverage gaps or PSD2 consent friction | High | Multi-aggregator strategy, file import fallback, proactive consent renewal |
| R5 | Incumbent price response or bundling | High | Compete on automation depth and mobile, not price; make migration in and out genuinely easy |
| R6 | Accounting firms resist a product that empowers clients directly | Medium | Firm portal is a first-class product, not an afterthought; firms keep control of what clients can do |
| R7 | ViDA scope or timing shifts | Medium | Structured-first data model means changes are configuration and mapping, not re-architecture |
| R8 | Mobile app rejected or delayed by store review | Medium | Early review-guideline compliance pass, demo account, no borderline billing patterns |
| R9 | Seasonal load at VAT deadlines degrades service | Medium | Load test at 10x, autoscaling, deadline freeze on deployments |
| R10 | Migration from incumbents is harder than promised | Medium | Build and test importers against real exports before GA; publish honest limits |

---

## 22. Definition of production ready

GA is blocked until every line below is true and evidenced.

**Functional**
- [ ] All **M** requirements in §6–§8 implemented and accepted
- [ ] A full fiscal year processed end to end in staging with real-shaped data: opening balance, postings, VAT returns for all periods, year-end close, XAF export validated against schema
- [ ] Live VAT return successfully filed via Digipoort in the production channel
- [ ] Peppol send and receive verified against certified test endpoints and a live counterparty
- [ ] Migration importers validated against real exports from at least three competing packages
- [ ] Both account models exercised end to end: a firm managing 20+ client administrations, and a standalone ZZP and BV running their own books
- [ ] Receipt capture verified across camera and upload, on iOS and Android, including HEIC, multi-page PDF, offline queue and duplicate detection
- [ ] Invoice designer verified: every layout and font combination renders identically in preview and PDF, passes PDF/A-3 validation, and cannot be saved in a statutorily non-compliant state
- [ ] Client access profiles verified, including that the IAM-105 rights floor cannot be removed by any firm setting or API call

**Security and access**
- [ ] Independent penetration test complete; all critical and high findings closed, mediums with owner and date
- [ ] Tenant isolation tests passing on 100% of endpoints in CI
- [ ] No standing internal access to customer data; JIT flow live and audited
- [ ] MFA enforced for all write-capable users; passkeys live
- [ ] Audit log immutability verified by attempted tamper test
- [ ] Key management, rotation and emergency rotation procedures tested
- [ ] Threat model reviewed and signed off for every subsystem

**Privacy**
- [ ] DPIA complete and signed off
- [ ] Record of Processing Activities complete
- [ ] DPA and sub-processor list published
- [ ] Retention and deletion jobs verified end to end, including backup purge
- [ ] Data subject request tooling tested against each right

**Reliability**
- [ ] Load test at 10x projected peak sustained without SLO breach
- [ ] DR failover exercised, RTO and RPO met and documented
- [ ] Backup restore drill passed within the last 90 days
- [ ] Runbooks written for every alert; on-call rotation staffed
- [ ] Error budgets and SLOs agreed and instrumented

**Clients**
- [ ] Web app WCAG 2.2 AA audited
- [ ] Moderated usability testing passed against FR-UX-001 with ≥ 80% unassisted task completion
- [ ] Every user-facing string present in both Dutch and English; CI completeness check green
- [ ] Google sign-in, passkey and password paths tested including account linking and Google-only lockout prevention (IAM-010f)
- [ ] iOS and Android apps approved in both stores, phased rollout configured
- [ ] Forced-upgrade and feature-flag kill switches tested in production
- [ ] Crash-free session rate ≥ 99.5% in beta

**Commercial and operational**
- [ ] Billing, dunning, upgrade, downgrade and cancellation flows tested end to end
- [ ] Support knowledge base live in Dutch and English
- [ ] Status page and incident communication process live
- [ ] Terms, privacy notice and DPA legally reviewed
- [ ] Exit path proven: a customer can export everything and leave without support involvement

---

## 23. Open questions

| # | Question | Owner | Needed by |
|---|---|---|---|
| ~~Q1~~ | ~~Direct-to-SMB, firm-led, or both?~~ **Resolved:** both, from P0. Firm-led and self-managed are two entry paths into one product; tenancy, IAM and the client access profile model in §8.6 are built for this from the start. | Product | Closed |
| Q2 | Build the Peppol Access Point or partner with a certified provider? | Engineering | Before P2 |
| Q3 | Which PSD2 aggregator, and is a second one needed for coverage? | Partnerships | Before P1 |
| Q4 | Does LEDGR obtain its own AISP licence eventually, or stay dependent on a partner? | Legal / Exec | Before P3 |
| Q5 | Managed document-AI service or self-hosted models? Cost, accuracy and EU residency trade-off. | Engineering | Before P1 |
| Q6 | Pricing model: per administration, per transaction volume, per user, or hybrid? | Commercial | Before P2 |
| Q7 | Is React Native sufficient for the mobile performance targets, or is fully native required? | Mobile | Before P1 |
| Q8 | Second market after NL — Belgium (regulatory proximity, active B2B mandate) or Germany (size)? | Exec | Before P4 |
| Q9 | Does the platform ever hold client money or initiate payments directly, or remain strictly a PISP consumer? | Legal | Before P2 |
| Q10 | Do we add Microsoft and Apple sign-in alongside Google, and when? Apple sign-in becomes mandatory on iOS the moment any third-party social login ships in the app. | Product / Mobile | Before P1 |
| Q11 | Where a client and their firm disagree over access, whose position prevails beyond the rights floor in IAM-105? Needs a documented policy before firms onboard at volume. | Legal / Product | Before P2 |
| Q12 | Is Dutch or English the default UI language for a firm's staff users, given many Dutch firms work bilingually? | Product | Before P0 |
| Q13 | P0 camera capture requires a mobile client. Installable PWA now with native apps at P2, or native from the start with a longer P0? | Mobile / Product | Before P0 |
| Q14 | Which 8–12 typefaces ship in the designer, and are they self-hosted with embedding rights cleared for commercial PDF distribution? | Design / Legal | Before P0 |
| Q15 | Is PDF rendering built in-house or on a managed service? It must run in-region (PRIV-010) and produce byte-identical output to the live preview (FR-TPL-008). | Engineering | Before P0 |

---

## Appendix A — Role and permission matrix

Legend: **F** full, **R** read only, **C** conditional (limited by scope, amount or attribute), **—** no access.

| Capability | Owner | Org Admin | Security Admin | Billing Admin | Accountant | Bookkeeper | Approver | Invoicer | Expense Submitter | Viewer |
|---|---|---|---|---|---|---|---|---|---|---|
| Manage organization settings | F | F | — | — | — | — | — | — | — | — |
| Create/delete administrations | F | F | — | — | — | — | — | — | — | — |
| Invite users, assign roles | F | F | — | — | — | — | — | — | — | — |
| Manage security policy (MFA, sessions, IP) | F | — | F | — | — | — | — | — | — | — |
| Run access reviews | F | R | F | — | — | — | — | — | — | — |
| Read audit log | F | R | F | — | R | — | — | — | — | R |
| Manage subscription and billing | F | — | — | F | — | — | — | — | — | — |
| View chart of accounts | F | — | — | — | F | F | R | R | — | R |
| Post journal entries | F | — | — | — | F | C | — | — | — | — |
| Reverse a posting | F | — | — | — | F | C | — | — | — | — |
| Lock / unlock periods | F | — | — | — | F | — | — | — | — | — |
| Year-end close | F | — | — | — | F | — | — | — | — | — |
| Create sales invoices | F | — | — | — | F | F | — | F | — | — |
| Send sales invoices | F | — | — | — | F | F | — | F | — | — |
| View purchase invoices | F | — | — | — | F | F | C | — | — | R |
| Code purchase invoices | F | — | — | — | F | F | — | — | — | — |
| Approve purchase invoices | F | — | — | — | F | — | C | — | — | — |
| Create payment batch | F | — | — | — | F | F | — | — | — | — |
| Release payment to bank | F | — | — | — | C | — | C | — | — | — |
| Connect / revoke bank consent | F | — | — | — | F | C | — | — | — | — |
| View bank transactions | F | — | — | — | F | F | — | — | — | R |
| Reconcile bank | F | — | — | — | F | F | — | — | — | — |
| Submit expenses | F | F | F | F | F | F | F | F | F | — |
| Approve expenses | F | — | — | — | F | — | C | — | — | — |
| Prepare VAT return | F | — | — | — | F | F | — | — | — | — |
| File VAT return | F | — | — | — | F | — | — | — | — | — |
| View reports | F | — | — | — | F | F | C | — | — | R |
| Export data | F | — | — | — | F | C | — | — | — | C |
| Manage API clients | F | F | R | — | — | — | — | — | — | — |
| Grant firm access to an administration | F | F | — | — | — | — | — | — | — | — |
| Revoke firm access | F | F | — | — | — | — | — | — | — | — |

Notes on the conditionals:
- **Bookkeeper posting** is limited to permitted journals and open periods.
- **Approver** is limited by amount ceiling and, optionally, cost centre.
- **Payment release** is subject to IAM-061 segregation: not the same person who approved it.
- **Viewer export** is permitted but always logged as a data-access event; it can be disabled per organization.
- **Accountant** never gains organization-level user management by virtue of being an accountant.

---

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| **Administration** | One legal entity's set of books |
| **AISP** | Account Information Service Provider, licensed to read bank account data under PSD2 |
| **BTW / OB** | Dutch VAT / turnover tax |
| **Digipoort** | Dutch government's message gateway for electronic filings, operated by Logius |
| **EN 16931** | European standard for the semantic data model of an electronic invoice |
| **ICP** | Declaration of intra-Community supplies |
| **KOR** | Kleineondernemersregeling, Dutch small business VAT scheme |
| **KvK** | Kamer van Koophandel, the Dutch chamber of commerce and business register |
| **NLCIUS** | Dutch Core Invoice Usage Specification, the national profile of EN 16931 |
| **OSS** | One Stop Shop, EU scheme for cross-border B2C VAT |
| **Peppol** | Pan-European network for exchanging structured business documents |
| **PISP** | Payment Initiation Service Provider, licensed to initiate payments under PSD2 |
| **RGS** | Referentie Grootboekschema, the Dutch standard reference chart of accounts (current version 3.8) |
| **SBR** | Standard Business Reporting, the Dutch standard for digital reporting to government and banks |
| **SoD** | Segregation of duties |
| **Suppletie** | A correction to a previously filed Dutch VAT return |
| **ViDA** | VAT in the Digital Age, the EU package mandating e-invoicing and digital reporting |
| **XAF** | XML Auditfile Financieel, the Dutch standard audit file format |
| **ZZP** | Zelfstandige zonder personeel, a sole trader without employees |
