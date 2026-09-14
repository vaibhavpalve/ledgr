import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { CaptureScreen } from "./CaptureScreen";
import { useSitting } from "./useSitting";

/**
 * `/capture` — FR-EXP-001's screen, exactly as ADR-036/047 built it, on
 * the session's own `SittingContext`. `useSitting`'s docstring said its
 * context would stay a prop "when there is [an active-client endpoint]",
 * and it has: the hook still receives it, from the one place that knows
 * the session, and never reaches for it.
 */
export function CaptureRoute() {
  const { sittingContext } = useAdministration();
  const { capture, queue, decode } = useServices();
  const sitting = useSitting({ context: sittingContext, queue, api: capture });

  return <CaptureScreen sitting={sitting} queue={queue} decode={decode} />;
}
