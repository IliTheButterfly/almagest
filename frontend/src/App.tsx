/**
 * The shell: a sticky header, a nav that is thumb-reachable on a phone and a tab
 * strip on a desktop, and the routed screen.
 *
 * The routes are not free choices. `/parts/:id`, `/locations/:id`, `/lots/:id`,
 * `/scan` and `/provision` are the **redirect targets of the backend's
 * `/s/{short_id}` route**, so a tapped NFC tag or a scanned QR lands on one of
 * them. Renaming any of them dead-ends every physical tag already stuck to a
 * drawer.
 */

import { useSyncExternalStore } from "react";
import { NavLink, Route, Routes } from "react-router-dom";

import { intakeQueue } from "./lib/intake/queue";
import { IntakeQueueScreen } from "./screens/IntakeQueueScreen";
import { LocationScreen } from "./screens/LocationScreen";
import { LotScreen } from "./screens/LotScreen";
import { NotFoundScreen } from "./screens/NotFoundScreen";
import { PartScreen } from "./screens/PartScreen";
import { ProvisionScreen } from "./screens/ProvisionScreen";
import { ScanScreen } from "./screens/ScanScreen";
import { SearchScreen } from "./screens/SearchScreen";
import { TreeScreen } from "./screens/TreeScreen";

function usePendingCount(): number {
  return useSyncExternalStore(
    (listener) => intakeQueue.subscribe(listener),
    () => intakeQueue.size,
    () => 0,
  );
}

export function App() {
  const pending = usePendingCount();

  return (
    <div className="app">
      <header className="app-header">
        <NavLink to="/search" className="brand">
          Almagest
        </NavLink>
        <span className="spacer" />
        <span className="muted-note">component inventory</span>
      </header>

      <nav className="app-nav" aria-label="Main">
        <NavLink to="/search">Search</NavLink>
        <NavLink to="/tree">Storage</NavLink>
        <NavLink to="/scan">Scan</NavLink>
        <NavLink to="/intake">Intake{pending > 0 ? ` (${pending})` : ""}</NavLink>
      </nav>

      <main className="app-main">
        <Routes>
          <Route index element={<SearchScreen />} />
          <Route path="/search" element={<SearchScreen />} />
          <Route path="/tree" element={<TreeScreen />} />
          <Route path="/scan" element={<ScanScreen />} />
          <Route path="/intake" element={<IntakeQueueScreen />} />
          {/* The `/s/{short_id}` redirect targets — these paths are physical. */}
          <Route path="/parts/:partId" element={<PartScreen />} />
          <Route path="/locations/:locationId" element={<LocationScreen />} />
          <Route path="/lots/:lotId" element={<LotScreen />} />
          <Route path="/provision" element={<ProvisionScreen />} />
          <Route path="*" element={<NotFoundScreen />} />
        </Routes>
      </main>
    </div>
  );
}
