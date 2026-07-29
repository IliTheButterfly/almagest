/**
 * Draw the room, and put the containers in it — the editing half of ADR 0009.
 *
 * Iliana: *"For the room layout, we should be able to draw a room and layout
 * containers."* Two facts, two halves of this panel, exactly as the schema splits
 * them: the **drawing** (walls, doors, the bench that holds nothing) is geometry
 * that is not a container and saves through `PUT …/plan/shapes` as a whole; a
 * **placement** is a coordinate on a child and saves through `PUT
 * …/plan/placements`, batched.
 *
 * **One save for a whole rearrangement.** Dragging five cabinets sends one
 * request, and only the ones that actually moved — `placementDiff` is what decides
 * that, and `unplace_location_ids` is how "back to the tray" is said, because no
 * coordinate is what nowhere means. Per-drag writes would make a five-box
 * rearrangement five requests that can partially fail and leave a room in a state
 * nobody authored.
 *
 * **Nothing here is pointer-only.** It is used on a phone at the shelf, so a box
 * is a finger-sized drag target — and it is also a `<button>`, so it is reachable
 * by Tab and moves under the arrow keys, with numeric millimetre fields beside it
 * for the case where a drag is not precise enough or not possible. Drawing a wall
 * likewise has both a tap and a pair of coordinate fields, and the rectangular
 * room is one button because that is what most rooms are.
 *
 * **This knows nothing about depth**, and must not learn: a room at the top of the
 * tree, a shelf two levels down and a drawer whose owner wants a plan of it are
 * the same code path. Nothing is validated against `child_view` either, for ADR
 * 0006's reason — refusing to let somebody draw would be the editor overruling the
 * person holding the furniture. What a plan *is drawn on* is decided by the level's
 * own `effective_child_view` when the page renders it, one call, at every depth.
 *
 * Known limits, both inherited from the ADR and stated rather than papered over: a
 * rotated box's *extent* stays axis-aligned (rotation is drawn, not applied), and
 * overlapping is allowed — a box does sit on a bench.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { boxClass, boxLabel } from "./FloorPlan";
import { ErrorBanner, Loading, Notice } from "./Feedback";
import { PlanSurface, planBoxStyle } from "./PlanSurface";
import {
  getLocationPlan,
  getLocationTree,
  setLocationPlanPlacements,
  setLocationPlanShapes,
  type LocationNode,
  type LocationRead,
  type LocationTree,
  type PlanShapeKind,
  type RoomPlanRead,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import {
  CLOSED_BY_DEFAULT,
  clampCoord,
  DEFAULT_GRID_MM,
  footprintOf,
  formatMm,
  frameCentre,
  frameOf,
  GRID_CHOICES,
  MIN_SHAPE_POINTS,
  placementDiff,
  placementDraftsFrom,
  placementsToRequest,
  shapeDraftsFrom,
  SHAPE_KINDS,
  SHAPE_LABELS,
  shapesDirty,
  shapesToRequest,
  snapMm,
  type PlacementDraft,
  type PlanShapeDraft,
} from "../lib/locations/roomPlan";
import { uuid4 } from "../lib/scan/session";

export function RoomPlanPanel({
  location,
  onSaved,
  onDirtyChange,
}: {
  location: LocationRead;
  onSaved: () => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const plan = useAsync<RoomPlanRead>(() => getLocationPlan(location.id), [location.id]);
  // The children's *names*, which the plan response does not carry: it answers
  // where things stand, and the tree answers what they are called.
  const tree = useAsync<LocationTree>(() => getLocationTree(location.id), [location.id]);
  const nodes = useMemo(
    () => (tree.data?.nodes ?? []).filter((node) => node.parent_id === location.id),
    [tree.data, location.id],
  );

  if (plan.error !== null || tree.error !== null) {
    return (
      <ErrorBanner
        error={plan.error ?? tree.error}
        fallback="This container's plan could not be loaded."
      />
    );
  }
  if (plan.data === null || tree.data === null) {
    return <Loading what="the plan" />;
  }
  return (
    <PlanEditor
      location={location}
      plan={plan.data}
      nodes={nodes}
      onDirtyChange={onDirtyChange}
      onSaved={() => {
        plan.reload();
        onSaved();
      }}
    />
  );
}

/**
 * What one drawn thing is called mid-sentence.
 *
 * Separate from `SHAPE_LABELS`, which is a menu entry with an explanation in it —
 * "Start drawing a Fixture — a sink, a pillar, a bench that holds nothing" is not
 * a button, and "a outline" is not English.
 */
const SHAPE_NOUN: Readonly<Record<PlanShapeKind, string>> = {
  outline: "the outline",
  wall: "a wall",
  door: "a door",
  window: "a window",
  fixture: "a fixture",
  zone: "a zone",
};

/** Client-local shape ids, which are never sent — see `PlanShapeDraft`. */
function idFactory(): () => string {
  let next = 0;
  return () => `draft-${next++}`;
}

function PlanEditor({
  location,
  plan,
  nodes,
  onSaved,
  onDirtyChange,
}: {
  location: LocationRead;
  plan: RoomPlanRead;
  nodes: readonly LocationNode[];
  onSaved: () => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const baselineShapes = useMemo(() => shapeDraftsFrom(plan.shapes, idFactory()), [plan.shapes]);
  const baselinePlacements = useMemo(() => placementDraftsFrom(plan.placements), [plan.placements]);

  const [shapes, setShapes] = useState<readonly PlanShapeDraft[]>(baselineShapes);
  const [placements, setPlacements] = useState<readonly PlacementDraft[]>(baselinePlacements);
  const [gridMm, setGridMm] = useState(DEFAULT_GRID_MM);
  const [selected, setSelected] = useState<number | null>(null);
  const [kind, setKind] = useState<PlanShapeKind>("outline");
  const [drawing, setDrawing] = useState<PlanShapeDraft | null>(null);
  const [pointX, setPointX] = useState("0");
  const [pointY, setPointY] = useState("0");
  const [roomWidth, setRoomWidth] = useState("4000");
  const [roomDepth, setRoomDepth] = useState("3000");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);

  const surface = useRef<HTMLDivElement | null>(null);
  const nextId = useRef(idFactory());
  /** A box that has just appeared and should get the caret — see `place`. */
  const wantsFocus = useRef<number | null>(null);

  // The line being drawn is drawn *with* the finished ones, so a wall in progress
  // is visible and sizes the frame like any other.
  const shown = useMemo(
    () => (drawing === null ? shapes : [...shapes, drawing]),
    [shapes, drawing],
  );
  const frame = useMemo(() => frameOf(shown, placements, gridMm), [shown, placements, gridMm]);

  const diff = useMemo(
    () => placementDiff(baselinePlacements, placements),
    [baselinePlacements, placements],
  );
  const dirty =
    shapesDirty(baselineShapes, shapes) || diff.changed.length > 0 || diff.unplaced.length > 0;
  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const boxes = placements.flatMap((placement) => {
    const node = byId.get(placement.locationId);
    return node === undefined ? [] : [{ placement, node }];
  });
  const placedIds = new Set(placements.map((placement) => placement.locationId));
  const unplaced = nodes.filter((node) => !placedIds.has(node.id));
  const selectedBox = boxes.find(({ placement }) => placement.locationId === selected) ?? null;

  // Focus follows a box that was just created, because the button that created it
  // (in the tray) is gone from the DOM the moment it succeeds — and a keyboard user
  // whose caret fell to `<body>` has lost the thing they just placed.
  useEffect(() => {
    const wanted = wantsFocus.current;
    if (wanted === null) {
      return;
    }
    wantsFocus.current = null;
    surface.current?.querySelector<HTMLElement>(`[data-placement="${wanted}"]`)?.focus();
  });

  function patch(locationId: number, changes: Partial<PlacementDraft>): void {
    setPlacements((list) =>
      list.map((placement) =>
        placement.locationId === locationId ? { ...placement, ...changes } : placement,
      ),
    );
  }

  function nudge(placement: PlacementDraft, dxMm: number, dyMm: number): void {
    patch(placement.locationId, {
      xMm: clampCoord(snapMm(placement.xMm + dxMm, gridMm)),
      yMm: clampCoord(snapMm(placement.yMm + dyMm, gridMm)),
    });
  }

  /** Out of the tray and onto the plan, in the middle of what is being looked at. */
  function place(node: LocationNode): void {
    const centre = frameCentre(frame, gridMm);
    setPlacements((list) => [
      ...list,
      {
        locationId: node.id,
        xMm: centre.xMm,
        yMm: centre.yMm,
        rotationDeg: 0,
        // Null, not a guess: the container type's own size is the common case, and
        // inventing a number here would override it with a worse one.
        widthMm: null,
        depthMm: null,
      },
    ]);
    setSelected(node.id);
    wantsFocus.current = node.id;
    setResult(null);
  }

  /** Back to the tray. Nowhere is its own state, never a coordinate of (0, 0). */
  function unplace(locationId: number): void {
    setPlacements((list) => list.filter((placement) => placement.locationId !== locationId));
    setSelected(null);
  }

  /**
   * A drag, in millimetres.
   *
   * The pointer is mapped through the surface's own bounding box, so "which box did
   * I grab" is answered by the DOM as it is everywhere else — there is no hit
   * testing here. The frame is captured at pointer-down: it is *derived* from what
   * is drawn, so dragging past the previous edge re-scales the picture, and
   * re-reading it mid-drag would make the box chase the cursor.
   *
   * Window listeners rather than `setPointerCapture`, which jsdom does not
   * implement — a drag that cannot be tested is a drag that breaks quietly.
   */
  function startDrag(event: ReactPointerEvent<HTMLElement>, placement: PlacementDraft): void {
    const rect = surface.current?.getBoundingClientRect();
    setSelected(placement.locationId);
    if (rect === undefined || rect.width === 0 || rect.height === 0) {
      return;
    }
    const perPxX = frame.widthMm / rect.width;
    const perPxY = frame.depthMm / rect.height;
    const fromX = event.clientX;
    const fromY = event.clientY;
    const originX = placement.xMm;
    const originY = placement.yMm;

    function onMove(move: PointerEvent): void {
      patch(placement.locationId, {
        xMm: clampCoord(snapMm(originX + (move.clientX - fromX) * perPxX, gridMm)),
        yMm: clampCoord(snapMm(originY + (move.clientY - fromY) * perPxY, gridMm)),
      });
    }
    function onUp(): void {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  function onBoxKey(event: ReactKeyboardEvent<HTMLElement>, placement: PlacementDraft): void {
    // Shift is a ten-step stride, so crossing a room does not take forty presses.
    const step = event.shiftKey ? gridMm * 10 : gridMm;
    const moves: Readonly<Record<string, readonly [number, number]>> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const move = moves[event.key];
    if (move === undefined) {
      return;
    }
    event.preventDefault();
    // Never `stopPropagation`: the panel's own Tab and Escape handling lives on the
    // document, and swallowing keys here is how a focus trap loses its exit.
    nudge(placement, move[0], move[1]);
  }

  function addPoint(xMm: number, yMm: number): void {
    setDrawing((line) =>
      line === null
        ? null
        : {
            ...line,
            points: [...line.points, { xMm: clampCoord(xMm), yMm: clampCoord(yMm) }],
          },
    );
  }

  function onSurfacePointerDown(event: ReactPointerEvent<HTMLDivElement>): void {
    if (drawing === null) {
      return;
    }
    const rect = surface.current?.getBoundingClientRect();
    if (rect === undefined || rect.width === 0 || rect.height === 0) {
      return;
    }
    const fx = (event.clientX - rect.left) / rect.width;
    const fy = (event.clientY - rect.top) / rect.height;
    addPoint(
      snapMm(frame.minXMm + fx * frame.widthMm, gridMm),
      snapMm(frame.minYMm + fy * frame.depthMm, gridMm),
    );
  }

  function startLine(): void {
    setDrawing({
      id: nextId.current(),
      kind,
      label: null,
      isClosed: CLOSED_BY_DEFAULT[kind],
      // Left null so the server keeps saying "unmeasured" rather than recording a
      // number nobody gave it; the renderer draws a nominal width for the kind.
      thicknessMm: null,
      points: [],
    });
    setResult(null);
  }

  function finishLine(): void {
    // A one-point line is dropped rather than sent: the server refuses it
    // (`PLAN_MIN_POINTS`), and a 422 for "you tapped once" is not a message anybody
    // can act on.
    if (drawing !== null && drawing.points.length >= MIN_SHAPE_POINTS) {
      const line = drawing;
      setShapes((list) => [...list, line]);
    }
    setDrawing(null);
  }

  /** The common case, in one press: most rooms are a rectangle. */
  function addRectangle(): void {
    const width = snapMm(Math.abs(Number(roomWidth) || 0), gridMm);
    const depth = snapMm(Math.abs(Number(roomDepth) || 0), gridMm);
    if (width <= 0 || depth <= 0) {
      setError(new Error("A rectangle needs a width and a depth in millimetres."));
      return;
    }
    setError(null);
    setShapes((list) => [
      ...list,
      {
        id: nextId.current(),
        kind: "outline",
        label: null,
        isClosed: true,
        thicknessMm: null,
        points: [
          { xMm: 0, yMm: 0 },
          { xMm: width, yMm: 0 },
          { xMm: width, yMm: depth },
          { xMm: 0, yMm: depth },
        ],
      },
    ]);
  }

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const wrote: string[] = [];
      if (shapesDirty(baselineShapes, shapes)) {
        await setLocationPlanShapes(location.id, {
          shapes: shapesToRequest(shapes),
          client_op_id: uuid4(),
        });
        wrote.push("the drawing");
      }
      // One request for every move, and only the ones that moved.
      if (diff.changed.length > 0 || diff.unplaced.length > 0) {
        await setLocationPlanPlacements(location.id, {
          placements: placementsToRequest(diff.changed),
          unplace_location_ids: [...diff.unplaced],
          client_op_id: uuid4(),
        });
        wrote.push(
          `${diff.changed.length} placement(s)` +
            (diff.unplaced.length === 0 ? "" : ` and ${diff.unplaced.length} back in the tray`),
        );
      }
      setResult(wrote.length === 0 ? "Nothing had changed." : `Saved ${wrote.join(" and ")}.`);
      onSaved();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="row">
        <label className="field" style={{ flex: "0 0 auto" }}>
          <span>Snap to</span>
          <select
            value={String(gridMm)}
            onChange={(event) => setGridMm(Number(event.target.value))}
          >
            {GRID_CHOICES.map((step) => (
              <option key={step} value={String(step)}>
                {formatMm(step)}
              </option>
            ))}
          </select>
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span>What to draw next</span>
          <select
            value={kind}
            disabled={drawing !== null}
            onChange={(event) => setKind(event.target.value as PlanShapeKind)}
          >
            {SHAPE_KINDS.map((value) => (
              <option key={value} value={value}>
                {SHAPE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      </div>

      <PlanSurface
        frame={frame}
        shapes={shown}
        gridMm={gridMm}
        label={`Plan of ${location.name}`}
        surfaceRef={surface}
        onPointerDown={onSurfacePointerDown}
        capturesTouch={drawing !== null}
        showVertices
      >
        {boxes.map(({ placement, node }) => (
          <button
            key={node.id}
            type="button"
            data-placement={placement.locationId}
            className={
              boxClass(placement, node, "plan-box plan-box-edit") +
              (selected === placement.locationId ? " plan-box-selected" : "")
            }
            style={planBoxStyle(placement, frame)}
            aria-pressed={selected === placement.locationId}
            aria-label={boxLabel(placement, node)}
            onPointerDown={(event) => startDrag(event, placement)}
            onFocus={() => setSelected(placement.locationId)}
            onClick={() => setSelected(placement.locationId)}
            onKeyDown={(event) => onBoxKey(event, placement)}
          >
            <span className="plan-box-name">{node.name}</span>
          </button>
        ))}
      </PlanSurface>

      <p className="muted-note" style={{ margin: 0 }}>
        Drag a box, or tab to it and move it with the arrow keys — one grid step a press, ten
        with Shift. Overlaps are allowed: a box really does sit on a bench, and geometry here is
        advisory exactly as capacity is. Nothing is saved until you press Save.
      </p>

      {/* ------------------------------------------------------- the drawing --- */}
      <div className="plan-tools stack">
        <h4 style={{ margin: 0 }}>The room itself</h4>
        {drawing === null ? (
          <>
            <div className="row">
              <button type="button" onClick={startLine}>
                Start drawing {SHAPE_NOUN[kind]}
              </button>
              <span className="spacer" />
            </div>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                <span>Room width (mm)</span>
                <input
                  inputMode="numeric"
                  value={roomWidth}
                  onChange={(event) => setRoomWidth(event.target.value)}
                />
              </label>
              <label className="field" style={{ flex: 1 }}>
                <span>Room depth (mm)</span>
                <input
                  inputMode="numeric"
                  value={roomDepth}
                  onChange={(event) => setRoomDepth(event.target.value)}
                />
              </label>
              <button type="button" onClick={addRectangle}>
                Add a rectangular room
              </button>
            </div>
          </>
        ) : (
          <div className="stack">
            <Notice kind="info" title={`Drawing ${SHAPE_NOUN[drawing.kind]}`}>
              <p style={{ margin: 0 }}>
                Tap the plan to drop a corner, or type one in — {drawing.points.length} so far.
                {drawing.isClosed
                  ? " This one closes back to where it started."
                  : " This one is a run, not a loop."}
              </p>
            </Notice>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                <span>X (mm)</span>
                <input
                  inputMode="numeric"
                  value={pointX}
                  onChange={(event) => setPointX(event.target.value)}
                />
              </label>
              <label className="field" style={{ flex: 1 }}>
                <span>Y (mm)</span>
                <input
                  inputMode="numeric"
                  value={pointY}
                  onChange={(event) => setPointY(event.target.value)}
                />
              </label>
              <button
                type="button"
                onClick={() =>
                  addPoint(snapMm(Number(pointX) || 0, gridMm), snapMm(Number(pointY) || 0, gridMm))
                }
              >
                Add this corner
              </button>
            </div>
            <div className="row">
              <button
                type="button"
                onClick={() =>
                  setDrawing((line) =>
                    line === null ? null : { ...line, points: line.points.slice(0, -1) },
                  )
                }
                disabled={drawing.points.length === 0}
              >
                Undo the last corner
              </button>
              <button type="button" onClick={() => setDrawing(null)}>
                Throw this line away
              </button>
              <span className="spacer" />
              <button
                type="button"
                className="primary"
                onClick={finishLine}
                disabled={drawing.points.length < MIN_SHAPE_POINTS}
              >
                Finish this line
              </button>
            </div>
          </div>
        )}

        {shapes.length === 0 ? (
          <p className="dim" style={{ margin: 0 }}>
            Nothing is drawn yet. A plan with no walls still places containers — the walls are
            what make it recognisable as your room.
          </p>
        ) : (
          <ul className="list">
            {shapes.map((shape) => (
              <li key={shape.id}>
                <div className="row">
                  <span style={{ flex: 1 }}>
                    {SHAPE_LABELS[shape.kind]} · {shape.points.length} corner(s) ·{" "}
                    {shape.isClosed ? "closed" : "a run"}
                  </span>
                  <button
                    type="button"
                    onClick={() => setShapes((list) => list.filter((item) => item.id !== shape.id))}
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ------------------------------------------------------------ tray --- */}
      <div className="plan-tray stack">
        <h4 style={{ margin: 0 }}>Not placed yet ({unplaced.length})</h4>
        {unplaced.length === 0 ? (
          <p className="dim" style={{ margin: 0 }}>
            Everything in here has somewhere to stand.
          </p>
        ) : (
          <>
            <p className="muted-note" style={{ margin: 0 }}>
              These are inside this container and nowhere on the plan. They stay here rather than
              being parked at the origin, which would look authored and put every one of them in
              the same corner.
            </p>
            <ul className="list">
              {unplaced.map((node) => (
                <li key={node.id}>
                  <div className="row">
                    <span style={{ flex: 1 }}>{node.name}</span>
                    <button type="button" onClick={() => place(node)}>
                      Place {node.name}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      {/* -------------------------------------------------- the selected box --- */}
      {selectedBox !== null && (
        <div className="plan-inspector stack">
          <h4 style={{ margin: 0 }}>{selectedBox.node.name}</h4>
          <div className="row">
            <label className="field" style={{ flex: 1 }}>
              <span>Across, X (mm)</span>
              <input
                inputMode="numeric"
                value={String(selectedBox.placement.xMm)}
                onChange={(event) =>
                  patch(selectedBox.placement.locationId, {
                    xMm: clampCoord(Number(event.target.value) || 0),
                  })
                }
              />
            </label>
            <label className="field" style={{ flex: 1 }}>
              <span>Down, Y (mm)</span>
              <input
                inputMode="numeric"
                value={String(selectedBox.placement.yMm)}
                onChange={(event) =>
                  patch(selectedBox.placement.locationId, {
                    yMm: clampCoord(Number(event.target.value) || 0),
                  })
                }
              />
            </label>
          </div>
          <div className="row">
            <label className="field" style={{ flex: 1 }}>
              <span>Turned (degrees)</span>
              <input
                inputMode="numeric"
                value={String(selectedBox.placement.rotationDeg)}
                onChange={(event) =>
                  patch(selectedBox.placement.locationId, {
                    rotationDeg: Number(event.target.value) || 0,
                  })
                }
              />
            </label>
            <button
              type="button"
              onClick={() =>
                patch(selectedBox.placement.locationId, {
                  rotationDeg: selectedBox.placement.rotationDeg + 90,
                })
              }
            >
              Turn 90°
            </button>
          </div>
          <div className="row">
            <label className="field" style={{ flex: 1 }}>
              <span>Width (mm)</span>
              <input
                inputMode="numeric"
                placeholder={`${footprintOf(selectedBox.placement).widthMm} — from its type`}
                value={selectedBox.placement.widthMm === null ? "" : String(selectedBox.placement.widthMm)}
                onChange={(event) =>
                  patch(selectedBox.placement.locationId, {
                    widthMm: event.target.value.trim() === "" ? null : clampCoord(Number(event.target.value) || 0),
                  })
                }
              />
            </label>
            <label className="field" style={{ flex: 1 }}>
              <span>Depth (mm)</span>
              <input
                inputMode="numeric"
                placeholder={`${footprintOf(selectedBox.placement).depthMm} — from its type`}
                value={selectedBox.placement.depthMm === null ? "" : String(selectedBox.placement.depthMm)}
                onChange={(event) =>
                  patch(selectedBox.placement.locationId, {
                    depthMm: event.target.value.trim() === "" ? null : clampCoord(Number(event.target.value) || 0),
                  })
                }
              />
            </label>
          </div>
          <p className="muted-note" style={{ margin: 0 }}>
            Leave a size empty to use the container type's own — a Raaco is 306 mm wide and the
            shelf it is bolted to is not in the type library. A drawn footprint is a picture: a box
            turned 45° still reports an axis-aligned extent, and nothing here checks for overlap.
          </p>
          <div className="row">
            <button
              type="button"
              className="danger"
              onClick={() => unplace(selectedBox.placement.locationId)}
            >
              Return {selectedBox.node.name} to the tray
            </button>
            <span className="spacer" />
          </div>
        </div>
      )}

      <ErrorBanner error={error} fallback="The plan was not saved." />
      {result !== null && <Notice kind="ok">{result}</Notice>}

      <div className="row">
        <span className="muted-note" style={{ flex: 1 }}>
          {drawing !== null
            ? "Finish or throw away the line you are drawing first."
            : dirty
              ? "Not saved yet."
              : "Saved."}
        </span>
        <button
          type="button"
          className="primary"
          onClick={() => void save()}
          disabled={busy || !dirty || drawing !== null}
        >
          {busy ? "Saving…" : "Save the plan"}
        </button>
      </div>
    </div>
  );
}
