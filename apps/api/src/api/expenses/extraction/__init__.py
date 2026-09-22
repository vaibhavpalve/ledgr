"""Automatic reading of a captured invoice - FR-EXP-001c, FR-AP-002.

A captured invoice is read for its supplier, number, date, total and VAT rate,
and the answer pre-fills the draft expense. Reading is a convenience laid over a
path that never needs it: extraction may be switched off, unavailable, slow or
wrong, and none of that may stop an invoice arriving (FR-EXP-001c).

    model      what a reading is, and the checks a reading must pass
    ports      the `InvoiceExtractor` a provider implements (non-negotiable 4)
    vertex     Claude on Google Vertex AI, in an EU region
    service    reading a captured invoice into its expense
    build      the provider named in settings
"""
