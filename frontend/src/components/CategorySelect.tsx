/**
 * Which category a part is filed under.
 *
 * The taxonomy could be authored and then nothing could be put in it: `POST` and
 * `PATCH /api/parts` have always taken `category_id`, and no screen ever sent one,
 * so every part in the catalogue sat outside the tree — which also made every
 * field authored on a category apply to nothing. Filing a part *is* what makes its
 * category's fields reach it.
 *
 * One control, used at create time and on the part itself, because those are the
 * same decision made at two moments. It loads the tree itself rather than taking
 * it as a prop: three callers would otherwise each fetch it, and a stale list is
 * how a category authored a minute ago is missing from the picker.
 *
 * Indented by depth with non-breaking spaces rather than by `<optgroup>`: a
 * category is a *node*, not a group header — it is selectable at every level, and
 * an optgroup label is not.
 */

import { listPartCategories, type CategoryNode } from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

export function CategorySelect({
  value,
  onChange,
  label = "Category",
  hint,
}: {
  /** `null` is "not filed anywhere", which is a real state and the default. */
  readonly value: number | null;
  readonly onChange: (categoryId: number | null) => void;
  readonly label?: string;
  readonly hint?: string | undefined;
}) {
  const categories = useAsync<CategoryNode[]>(() => listPartCategories(), []);
  const nodes = categories.data ?? [];

  return (
    <>
      <label className="field">
        <span>{label}</span>
        <select
          value={value === null ? "" : String(value)}
          onChange={(event) =>
            onChange(event.target.value === "" ? null : Number(event.target.value))
          }
        >
          <option value="">Not filed under anything</option>
          {nodes.map((node) => (
            <option key={node.id} value={String(node.id)}>
              {" ".repeat(node.depth * 2)}
              {node.name}
            </option>
          ))}
        </select>
      </label>
      {hint !== undefined && (
        <p className="muted-note" style={{ margin: 0 }}>
          {hint}
        </p>
      )}
      {!categories.loading && nodes.length === 0 && (
        <p className="muted-note" style={{ margin: 0 }}>
          No categories exist yet. Part types is where they are made.
        </p>
      )}
    </>
  );
}
