import { useState } from "react";
import { Download } from "lucide-react";
import { useI18n } from "@ledgr/i18n";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import type { ExportKind } from "./api";

/**
 * ADR-089's two exports, beside the Grootboek's title: every posted line of the year and the
 * trial balance, as CSV a Dutch Excel opens as columns. What an accountant asks for first.
 */
export function ExportButtons() {
  const { t } = useI18n();
  const { administration, fiscalYear } = useAdministration();
  const { ledger } = useServices();
  const [busy, setBusy] = useState<ExportKind | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const download = async (kind: ExportKind) => {
    setBusy(kind);
    setProblem(null);
    try {
      const { blob, filename } = await ledger.downloadExport(
        administration.id,
        kind,
        fiscalYear.id,
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setBusy(null);
    }
  };

  return (
    <span className="ledger-exports">
      {(["journal", "trial-balance"] as const).map((kind) => (
        <button
          key={kind}
          type="button"
          className="button--quiet"
          disabled={busy !== null}
          data-testid={`ledger-export-${kind}`}
          onClick={() => void download(kind)}
        >
          <Download size={16} strokeWidth={1.8} aria-hidden="true" /> {t(`ledger.export.${kind}`)}
        </button>
      ))}
      {problem !== null ? (
        <span role="alert" className="caption">
          {problem}
        </span>
      ) : null}
    </span>
  );
}
