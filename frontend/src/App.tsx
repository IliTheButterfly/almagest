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

import { useEffect, useRef, useSyncExternalStore } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { Logo } from "./components/Logo";
import { ThemeToggle } from "./components/ThemeToggle";
import { WorkPanel } from "./components/WorkPanel";
import { useGlobalTagReader } from "./lib/scanctx/useScanContext";
import { carts } from "./lib/cart/registry";
import { describeTarget } from "./lib/cart/describe";
import { useCartSize } from "./lib/cart/useCart";
import { intakeQueue } from "./lib/intake/queue";
import { useFocusedTarget } from "./lib/projectcontext/hooks";
import { BomScreen } from "./screens/BomScreen";
import { BuildScreen } from "./screens/BuildScreen";
import { ContainerTypeScreen } from "./screens/ContainerTypeScreen";
import { ContainerTypesScreen } from "./screens/ContainerTypesScreen";
import { ChatScreen } from "./screens/ChatScreen";
import { DatasheetSearchScreen } from "./screens/DatasheetSearchScreen";
import { CapturesScreen } from "./screens/CapturesScreen";
import { IntakeActivityScreen } from "./screens/IntakeActivityScreen";
import { IntakeQueueScreen } from "./screens/IntakeQueueScreen";
import { LocationScreen } from "./screens/LocationScreen";
import { LotScreen } from "./screens/LotScreen";
import { ModelsScreen } from "./screens/ModelsScreen";
import { NewContainersScreen } from "./screens/NewContainersScreen";
import { NewContainerTypeScreen } from "./screens/NewContainerTypeScreen";
import { NotFoundScreen } from "./screens/NotFoundScreen";
import { PartScreen } from "./screens/PartScreen";
import { PartTypesScreen } from "./screens/PartTypesScreen";
import { ProjectScreen } from "./screens/ProjectScreen";
import { ProjectsScreen } from "./screens/ProjectsScreen";
import { ProvisionScreen } from "./screens/ProvisionScreen";
import { ReviewScreen } from "./screens/ReviewScreen";
import { ScanScreen } from "./screens/ScanScreen";
import { SearchScreen } from "./screens/SearchScreen";
import { StagingScreen } from "./screens/StagingScreen";
import { TreeScreen } from "./screens/TreeScreen";

/**
 * The old layout-editor URL, pointed at the panel it became.
 *
 * `replace` so Back goes where the user came from rather than bouncing through
 * the dead route again.
 */
function LayoutRedirect() {
  const { locationId } = useParams();
  return <Navigate to={`/locations/${locationId ?? ""}?edit=1&panel=layout`} replace />;
}

function usePendingCount(): number {
  return useSyncExternalStore(
    (listener) => intakeQueue.subscribe(listener),
    () => intakeQueue.size,
    () => 0,
  );
}

/**
 * The focused target, named in the header, on every screen.
 *
 * ADR 0010's first mitigation for the risk it creates: the focused tab decides
 * where a take is attributed, so a mode with no visible indicator is exactly the
 * failure the panel exists to prevent. This is the always-visible half of that —
 * the tab strip and the two collapsible sections are the panel's job; what belongs
 * here is the one fact every screen has to be able to state, plus how many lines
 * are waiting in it.
 */
function WorkingOn() {
  const focused = useFocusedTarget();
  const uncommitted = useCartSize(focused === null ? null : carts.for(focused));
  if (focused === null) {
    return null;
  }
  return (
    <span className="badge" title="Takes are attributed to this until you close it">
      {describeTarget(focused)}
      {uncommitted > 0 ? ` · ${uncommitted} to commit` : ""}
    </span>
  );
}

export function App() {
  const pending = usePendingCount();
  // Mounted here and nowhere else. A reader subscribed by a screen only works
  // while that screen is open, which is what made scanning a place you went to
  // rather than a way of answering the question in front of you.
  useGlobalTagReader();
  const navRef = useRef<HTMLElement | null>(null);
  const location = useLocation();

  /**
   * Keep the tab you are on visible.
   *
   * The bar scrolls sideways (see `.app-nav`), so on a phone the tab for the
   * screen you just opened can sit past the right edge — which reads as "that
   * destination is missing" rather than "scroll for it". Runs on every location
   * change, and is a no-op on a wide screen where nothing overflows.
   */
  useEffect(() => {
    const current = navRef.current?.querySelector<HTMLElement>('[aria-current="page"]');
    current?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [location.pathname]);

  return (
    <div className="app">
      <header className="app-header">
        {/* The wordmark is its own element so the gradient underline stays under
            the text and does not run beneath the mark. */}
        <NavLink to="/search" className="brand">
          <Logo />
          <span>Almagest</span>
        </NavLink>
        <span className="spacer" />
        <WorkingOn />
        <ThemeToggle />
      </header>

      <nav className="app-nav" aria-label="Main" ref={navRef}>
        <NavLink to="/search">Search</NavLink>
        {/* Second, not seventh. Eleven tabs do not fit a phone: at 430 CSS px
            the strip shows about six, and Scan used to sit past the right edge
            on every fresh load — so the bench's most-used entry point that is
            not a tag tap was invisible until you thought to swipe a nav bar. The
            `scrollIntoView` below only helps once you are already on it. */}
        <NavLink to="/scan">Scan</NavLink>
        <NavLink to="/tree">Storage</NavLink>
        <NavLink to="/intake">Intake{pending > 0 ? ` (${pending})` : ""}</NavLink>
        <NavLink to="/chat">Ask</NavLink>
        <NavLink to="/datasheets">Datasheets</NavLink>
        {/* "Types" was jargon: the tab that lets you make your own cabinets and
            drawers read as a settings page, and was reported twice as "I still
            can't create my own containers". "Containers" is what the screen is
            for; the URL stays `/container-types`, which nothing physical points
            at. */}
        <NavLink to="/container-types">Containers</NavLink>
        {/* Beside Containers, because they are the same sort of job — authoring
            the vocabulary rather than moving stock — and because the two read as
            a pair: what a container is, and what a part is. */}
        <NavLink to="/part-types">Part types</NavLink>
        <NavLink to="/projects">Projects</NavLink>
        {/* Staging empties the inbox that Intake fills, and they used to be
            adjacent for that reason — "next to the tab that fills it or nobody
            goes looking". Promoting Scan cost that: Intake is now 4th and on
            screen at 430 px, Staging is 9th and is not. Recorded rather than
            papered over, because it is a real trade and the next person to
            touch this bar should know it was made deliberately. The inbox is
            also reachable from Storage, which is what makes it survivable. */}
        <NavLink to="/staging">Staging</NavLink>
        <NavLink to="/review">Review</NavLink>
        {/* Next to Review because it is the same kind of errand: looking again at
           something a decision was deferred on. */}
        <NavLink to="/captures">Captures</NavLink>
      </nav>

      {/*
        * The page and the panel, side by side from 60rem up and stacked below it.
        *
        * The panel comes *after* the page in the DOM on purpose: it is a
        * companion to whatever screen you are on, so a phone should reach the
        * screen it navigated to first and the running record under it. On a wide
        * screen the grid puts it on the right without changing that order, which
        * is also the reading order a screen reader gets.
        */}
      <div className="app-body">
        <main className="app-main">
          <Routes>
            <Route index element={<SearchScreen />} />
            <Route path="/search" element={<SearchScreen />} />
            {/* Phase 4's standalone value: full-text search over every stored
                PDF's extracted text, not part fields — a different question
                from `/search`, not a mode of it. Not a scan target. */}
            <Route path="/chat" element={<ChatScreen />} />
            {/* A conversation is linkable: same screen, thread id in the URL. */}
            <Route path="/chat/:threadId" element={<ChatScreen />} />
            {/* What is running on the GPU, and the switch for each. No nav tab:
                the strip is already too long for a phone, and this is reached from
                the model picker on Ask — which is where somebody is standing when
                a model turns out not to be running. */}
            <Route path="/models" element={<ModelsScreen />} />
            <Route path="/datasheets" element={<DatasheetSearchScreen />} />
            <Route path="/tree" element={<TreeScreen />} />
            <Route path="/scan" element={<ScanScreen />} />
            <Route path="/intake" element={<IntakeQueueScreen />} />
            {/* One entry's whole story: what the browser read, what a model was told
                and answered, and what became of the part somebody accepted. A
                diagnostic read, reached from the row it is about — not a scan target,
                and deliberately not a place anything can be changed. */}
            <Route path="/intake/:entryId/activity" element={<IntakeActivityScreen />} />
            {/* Emptying the inbox. Not a scan target — reached from the tab, and from
                intake once a part has been parked there. */}
            <Route path="/staging" element={<StagingScreen />} />
            <Route path="/review" element={<ReviewScreen />} />
            {/* The photographs the scanner kept. Not a scan target: reached from
                the tab, and from the capture panel on Scan. */}
            <Route path="/captures" element={<CapturesScreen />} />
            {/* The `/s/{short_id}` redirect targets — these paths are physical. */}
            <Route path="/parts/:partId" element={<PartScreen />} />
            <Route path="/locations/:locationId" element={<LocationScreen />} />
            <Route path="/lots/:lotId" element={<LotScreen />} />
            <Route path="/provision" element={<ProvisionScreen />} />
            {/* Layout authoring — reached from the tab and from a container's own
                screen, never from a scanned tag. `/container-types/new` is listed
                before the parameterised route for readability only: React Router
                ranks a static segment above a dynamic one whatever the order. */}
            <Route path="/part-types" element={<PartTypesScreen />} />
            <Route path="/container-types" element={<ContainerTypesScreen />} />
            <Route path="/container-types/new" element={<NewContainerTypeScreen />} />
            <Route path="/container-types/:containerTypeId" element={<ContainerTypeScreen />} />
            {/* Collapsed into the container's own page's edit mode — Iliana asked
                to lose the page-per-editing-task. Kept as a redirect rather than
                deleted: the link is in browser histories and in her notes, and
                landing on "not found" would read as the feature having been
                removed. `?panel=layout` opens the panel it used to be. */}
            <Route path="/locations/:locationId/layout" element={<LayoutRedirect />} />
            {/* Creating real containers out of a *type*, which is the one create
                path that has no container page to live on: it is reached from the
                type library and from a type's own page, which know what to stamp and
                not where it goes, and from the empty tree, where no container exists
                yet. Adding into a container you are looking at is that container's
                own edit mode instead. Deliberately **not** under `/locations/...`:
                every path in that space is a `/s/{short_id}` redirect target, and
                this one is never reached from a tag. */}
            <Route path="/containers/new" element={<NewContainersScreen />} />
            {/* Projects, BOMs and builds — not a scan target; reached from the tab. */}
            <Route path="/projects" element={<ProjectsScreen />} />
            <Route path="/projects/:projectId" element={<ProjectScreen />} />
            <Route path="/projects/:projectId/bom" element={<BomScreen />} />
            <Route path="/builds/:buildId" element={<BuildScreen />} />
            <Route path="*" element={<NotFoundScreen />} />
          </Routes>
        </main>
        <WorkPanel />
      </div>
    </div>
  );
}
