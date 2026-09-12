/**
 * Whether the browser thinks it can reach the network — MOB-003.
 *
 * `navigator.onLine` is a weak signal and is used as one. It reports that an
 * interface is up, not that the API is reachable: a captive portal that has
 * not been agreed to, a hotel wifi, a VPN mid-handshake and a laptop connected
 * to a router with no uplink all report `true`. So this drives an ATTEMPT and
 * never a conclusion — the uploader treats a network failure while this says
 * `true` as an ordinary retryable error, and its backoff timer is what covers
 * a connection that came back without the event firing.
 *
 * `visibilitychange` is listened to alongside the `online` event because a
 * phone that was asleep in a pocket while the network returned often produces
 * no `online` event at all: the tab was frozen. Coming back to the foreground
 * is the moment worth re-checking, and it is a far more reliable signal than
 * the one named after the thing we want to know.
 */

import type { Cancel, Connectivity, Wakeup } from "@ledgr/offline-queue";

export class BrowserConnectivity implements Connectivity {
  get online(): boolean {
    // A browser without the property is treated as connected. Assuming
    // offline would leave the queue never attempting anything at all, which
    // fails in the direction that loses receipts.
    return typeof navigator === "undefined" || navigator.onLine !== false;
  }

  onOnline(listener: Wakeup): Cancel {
    const wake = () => {
      // `visibilitychange` fires on the way out as well as the way in, and a
      // drain started as the tab is hidden is one the browser is about to
      // freeze halfway through.
      if (document.visibilityState !== "visible") return;
      if (this.online) void listener();
    };
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }
}
