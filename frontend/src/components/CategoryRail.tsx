/**
 * Browse by type, DigiKey-style: the category tree with counts.
 *
 * The counts include descendants, computed server-side with one prefix match on
 * the cached `id_path` — the same mechanism search itself uses, so the number
 * beside "Passives" always agrees with what selecting it returns.
 *
 * A zero-count category is **disabled, not hidden**, and its count is struck
 * through. In a personal inventory most categories legitimately have zero, and
 * "you own none of these" is the single most useful thing this screen can say —
 * hiding it would make the taxonomy look like it changes shape as stock moves.
 */

import type { CategoryNode } from "../lib/api/client";

export function CategoryRail({
  categories,
  selected,
  onSelect,
}: {
  categories: readonly CategoryNode[] | null;
  selected: string;
  onSelect: (slug: string) => void;
}) {
  return (
    <div className="card">
      <h3>Type</h3>
      <ul className="rail" aria-label="Part categories">
        <li>
          <button
            type="button"
            aria-pressed={selected === ""}
            onClick={() => onSelect("")}
          >
            <span className="tick" aria-hidden="true" />
            <span>All parts</span>
          </button>
        </li>
        {(categories ?? []).map((category) => {
          const active = selected === category.slug;
          return (
            <li key={category.slug}>
              <button
                type="button"
                aria-pressed={active}
                // Nothing to narrow to, so the control says so rather than
                // leading somewhere empty. The current selection stays clickable
                // even at zero, or it could not be cleared.
                disabled={category.part_count === 0 && !active}
                style={{ paddingLeft: `${0.5 + category.depth * 0.85}rem` }}
                onClick={() => onSelect(active ? "" : category.slug)}
              >
                <span className="tick" aria-hidden="true" />
                <span>{category.name}</span>
                <span className="count" aria-label={`${category.part_count} parts`}>
                  {category.part_count}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {categories !== null && categories.length === 0 && (
        <p className="muted-note">No categories defined yet.</p>
      )}
    </div>
  );
}
