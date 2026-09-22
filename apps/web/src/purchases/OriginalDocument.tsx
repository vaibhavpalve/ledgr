import { useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";

import type { SalesInvoiceApi } from "../invoicing/api";

/**
 * The invoice as it was uploaded, shown beside the fields read from it.
 *
 * Fetched with `fetch()` into a `Blob` and shown from an object URL - never a
 * direct `<iframe src="/v1/...">` at the download endpoint, which answers with
 * `Content-Disposition: attachment` on purpose (SEC-005) so that nothing
 * navigates to a stored file. The same technique the old View screen used for
 * its documents.
 */
export function OriginalDocument({
  administrationId,
  documentId,
  api,
}: {
  administrationId: string;
  documentId: string;
  api: Pick<SalesInvoiceApi, "fetchDocumentBlob">;
}) {
  const { t } = useI18n();
  const [url, setUrl] = useState<string | null>(null);
  const [problem, setProblem] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setUrl(null);
    setProblem(false);
    api
      .fetchDocumentBlob(administrationId, documentId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setProblem(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [administrationId, api, documentId]);

  return (
    <section className="purchase-original" aria-label={t("capture.purchases.original")}>
      <h2>{t("capture.purchases.original")}</h2>
      {problem ? (
        <p role="alert" data-testid="purchase-original-error">
          {t("capture.purchases.original_error")}
        </p>
      ) : null}
      {!problem && url === null ? (
        <p role="status" data-testid="purchase-original-loading">
          {t("capture.purchases.original_loading")}
        </p>
      ) : null}
      {url !== null ? (
        <iframe
          className="purchase-original__frame"
          title={t("capture.purchases.original")}
          data-testid="purchase-original-frame"
          src={url}
        />
      ) : null}
    </section>
  );
}
