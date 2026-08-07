/**
 * Models — what is running on the GPU, and the switch for each.
 *
 * Reached from the model picker on Ask rather than from the nav bar. That is
 * deliberate: the tab strip already carries thirteen destinations and does not fit
 * a phone (see `App.tsx`), and this is a page somebody visits when a model is not
 * answering — which is exactly when they are looking at the picker.
 *
 * The panel is a component rather than inline here so the same control can be
 * dropped beside a failed answer later without a second implementation.
 */

import { ModelsPanel } from "../components/ModelsPanel";

export function ModelsScreen() {
  return (
    <div className="stack">
      <h2 style={{ margin: 0 }}>Models</h2>
      <p className="dim" style={{ margin: 0 }}>
        The models Almagest can ask, and which are loaded right now.
      </p>
      <ModelsPanel />
    </div>
  );
}
