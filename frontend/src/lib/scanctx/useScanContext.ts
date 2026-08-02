/**
 * Subscribing to the scan context, and feeding it.
 *
 * `useGlobalTagReader` is mounted **once, in the app shell**. That placement is
 * the feature: a reader subscribed by a screen only works while that screen is
 * open, which is what made scanning a destination instead of an input method.
 * Held at the shell, a tap on the Flipper reaches the panel whether you are
 * looking at a build, an intake queue or a part.
 *
 * It degrades to nothing when no bridge is running, which is almost every page
 * load — `useBridgeDevices` already returns an empty roster rather than
 * throwing, so there is no error path here and nothing to show.
 */

import { useEffect } from "react";
import { useSyncExternalStore } from "react";

import { resolveScan } from "../api/client";
import { uuid4 } from "../scan/session";
import { bridgeSource } from "../tags/bridge";
import { useBridgeDevices } from "../tags/useBridge";
import { scanContext, type ScanRecord } from "./store";

export function useScans(): readonly ScanRecord[] {
  return useSyncExternalStore(
    (listener) => scanContext.subscribe(listener),
    () => scanContext.list(),
    () => [],
  );
}

/** Whether anything could put a scan here — a reader is attached. */
export function useHasReader(): boolean {
  const { devices } = useBridgeDevices();
  return devices.some((device) => device.capabilities.readsNdef || device.capabilities.readsUid);
}

/**
 * Listen to every attached reader and resolve what they read into the context.
 *
 * Mount once. Resolving here rather than in the panel means the panel renders a
 * *fact* — "this is Workbench cabinet / 01" — instead of a payload it would have
 * to resolve itself on every render, and it means a field can ask "was a
 * container scanned?" without knowing how to talk to the resolver.
 */
export function useGlobalTagReader(): void {
  const { devices, connection } = useBridgeDevices();

  useEffect(() => {
    if (connection === null || devices.length === 0) {
      return;
    }
    const unsubscribes = devices
      .filter((device) => device.capabilities.readsNdef || device.capabilities.readsUid)
      .flatMap((device) => [
        // Lifted off. The row stays in the panel — it is still the last thing
        // you scanned — but nothing may claim any more that you are holding it.
        connection.onGone(device.deviceId, () => {
          scanContext.lifted(device.deviceId);
        }),
        bridgeSource(connection, device).subscribe((tap) => {
          // NDEF first, UID as the fallback: the resolver's own order, and what
          // makes a tag whose record was never written still identifiable.
          const code = tap.url ?? tap.shortId ?? tap.uid;
          if (code === null) {
            return;
          }
          void resolveScan({ code, symbology: "nfc" })
            .then((response) => {
              scanContext.add({
                id: uuid4(),
                at: Date.now(),
                code,
                symbology: "nfc",
                target: response.target ?? null,
                status: response.status,
                presentOn: device.deviceId,
              });
            })
            .catch(() => {
              // The bench loses its network more often than it loses its reader,
              // and a tap that reached the reader is still worth showing. It
              // lands unresolved rather than vanishing.
              scanContext.add({
                id: uuid4(),
                at: Date.now(),
                code,
                symbology: "nfc",
                target: null,
                status: "unmatched",
                presentOn: device.deviceId,
              });
            });
        }),
      ]);
    return () => {
      for (const unsubscribe of unsubscribes) {
        unsubscribe();
      }
    };
  }, [connection, devices]);
}
