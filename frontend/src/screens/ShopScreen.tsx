/**
 * Choosing parts out of your own stock — the second of ADR 0007's two ways.
 *
 * This screen is **the search screen**, unmodified, plus one control per row. Not
 * a picker, not a cut-down variant: "it's still not the same view as the search
 * tab" was the complaint, and the ADR's answer is that when the question is *what
 * do I have*, the facet counts, the category rail and the stock-per-row **are** the
 * answer, so a one-field box cannot stand in for them. What is added is a button;
 * everything that makes the view worth using is inherited by rendering
 * `SearchScreen` itself with a `rowAction`.
 *
 * Adding writes nothing. A row goes into the cart with no lot chosen, which is the
 * honest state — search knows what part you picked, not which reel it comes off —
 * and the cart screen says what that means for each destination.
 */

import { Link } from "react-router-dom";

import { shoppingCart } from "../lib/cart/cart";
import { useCartLines, useCartTarget } from "../lib/cart/useCart";
import type { PartSummary } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { describeTarget } from "../lib/cart/describe";
import { SearchScreen } from "./SearchScreen";

export function ShopScreen() {
  const lines = useCartLines();
  const target = useCartTarget();

  return (
    <div className="stack">
      <div className="card">
        <h1>Add parts to the cart</h1>
        <p className="muted-note">
          The same search as the Search tab — the type rail, the filters and the
          stock on every row all still apply, because deciding what to build with is
          mostly reading what you already have. Adding a part here writes nothing;
          the cart is committed in one go, and until then it is only a list.
        </p>
        <div className="row">
          <span className="badge">{describeTarget(target)}</span>
          <span className="spacer" />
          <Link to="/cart">
            {lines.length === 0
              ? "The cart is empty →"
              : `Review the cart (${lines.length}) →`}
          </Link>
        </div>
      </div>

      <SearchScreen rowAction={(part) => <AddToCart part={part} />} />
    </div>
  );
}

/**
 * One tap adds one; tapping again adds another.
 *
 * No quantity field on the row on purpose. A number input per row is four taps
 * before anything happens and, on a phone, sits in the touch target of the row's
 * own link; the quantity of a chosen part is also the thing most often revised
 * once the whole list is visible, which is the cart screen's job. So the row
 * commits to *this part, one more of it*, and reports the running total so a
 * second press is deliberate rather than a guess.
 */
function AddToCart({ part }: { part: PartSummary }) {
  const lines = useCartLines();
  const inCart = lines
    .filter((line) => line.partId === part.id)
    .reduce((total, line) => total + line.qtyMilli, 0);

  return (
    <div className="stack" style={{ flex: "0 0 auto", alignItems: "flex-end", gap: "0.2rem" }}>
      <button
        type="button"
        onClick={() =>
          shoppingCart.add({
            partId: part.id,
            partName: part.name,
            mpn: part.mpn,
            qtyMilli: 1000,
          })
        }
      >
        Add to cart
      </button>
      {inCart > 0 && (
        <span className="muted-note">{formatQty(inCart)} in the cart</span>
      )}
    </div>
  );
}
