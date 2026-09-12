"""PRIV-021..023: data subject requests, across whichever module owns the
resource a request names.

There is no service here, deliberately. An erasure request against a
document is a fact about FR-DOC-002's retention; one against a customer is a
fact about 0039's snapshot design (CMP-001). Both modules already own that
knowledge, so `api.documents.service.DocumentService.request_erasure` and
`api.customers.service.CustomerService.request_erasure` decide for
themselves - this package holds only the shared shape their answers take
(`api.privacy.model.ErasureDecision`), so a client reading either response
learns the same fields the same way.
"""
