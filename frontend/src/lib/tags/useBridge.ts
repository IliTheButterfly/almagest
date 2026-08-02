/**
 * The device bridge, as a React hook — the wire `bridge.ts` was written for and
 * did not have.
 *
 * ADR 0014 built both halves of this and connected neither: `deviceagent` has
 * published `device.attached` on `ws://127.0.0.1:8765` since it was written, and
 * `bridge.ts` has known how to read it, but nothing in the app ever called
 * `openBridge`. The module was tree-shaken out of every build — grep the deployed
 * bundle for `8765` and it is not there. So the ADR's stated consequence, *"a
 * laptop becomes a provisioning station"*, was true of the code and false of the
 * product, and the bench station — the machine holding the only reader in the
 * building — offered "Type the UID".
 *
 * **A bridge that is not running is not an error; it is Tuesday.** Almost every
 * page load is a phone with no bridge anywhere. So this hook must not warn, must
 * not retry aggressively, and must not delay anything: it returns an empty roster
 * and the caller renders exactly what it rendered before. `openBridge` already
 * owns the reconnect backoff; all this adds is a React lifetime and the ADR 0003
 * discipline that a reader appears in the UI **because a `device.attached`
 * arrived**, never because a capability flag permitted it.
 */

import { useEffect, useState } from "react";

import type { BridgeConnection, BridgeDevice, BridgeOptions } from "./bridge";
import { openBridge } from "./bridge";

/**
 * Attached bridged readers, live.
 *
 * Empty until something attaches, and empty for ever on a machine with no
 * bridge. The connection is opened once per mount and closed on unmount —
 * per-mount rather than module-global because a socket that outlives every
 * component is one nothing can be shown to have closed.
 */
export interface BridgeRoster {
  readonly devices: readonly BridgeDevice[];
  /**
   * The live connection, or `null` where none could be opened. Exposed because
   * `bridgeSource` binds a device to the connection it arrived on — a device id
   * alone is not enough to subscribe or to write.
   */
  readonly connection: BridgeConnection | null;
}

export function useBridgeDevices(options: BridgeOptions = {}): BridgeRoster {
  const [roster, setRoster] = useState<BridgeRoster>({
    devices: [],
    connection: null,
  });

  // `options` is a fresh object literal on most call sites, so it must not be a
  // dependency — it would reopen the socket on every render. The bridge URL is
  // not something that changes during a walk.
  useEffect(() => {
    let connection: BridgeConnection;
    try {
      connection = openBridge(options);
    } catch {
      // No `WebSocket` at all (a test environment, an exotic webview). Silence
      // is the specified behaviour.
      return;
    }
    const opened = connection;
    const unsubscribe = opened.onDevices((next) =>
      setRoster({ devices: next, connection: opened }),
    );
    // The roster may already be populated if a socket opened synchronously.
    setRoster({ devices: opened.devices(), connection: opened });
    return () => {
      unsubscribe();
      opened.close();
      setRoster({ devices: [], connection: null });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return roster;
}
