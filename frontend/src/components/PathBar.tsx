/**
 * The path above a page, on every page that has one.
 *
 * One component so that "where am I, and how do I get back up" looks and behaves
 * the same whether you are in a drawer, a project, a build or a lot. Before this
 * there were three answers on four screens: the storage tree had clickable
 * crumbs, the container page had the same path as dead text plus a lone "Up one
 * level" link, the picker had its own copy of the tree's crumbs, and everything
 * else had nothing — so the page a scanned tag lands on was the one you could not
 * navigate out of.
 *
 * Renders `lib/locations/trail`'s data and decides nothing about hierarchy
 * itself. Two crumb kinds, because a picker is not a page:
 *
 * - `to` is a `Link` — ordinary navigation.
 * - `onSelect` is a `button` — for a trail inside a form, where routing away
 *   would discard the quantity somebody already typed.
 *
 * The final crumb is the current thing: never a link, and marked
 * `aria-current="page"` so it is announced as where you are rather than read as
 * one more place you could go.
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { PathCrumb } from "../lib/locations/trail";

export interface PathBarProps {
  readonly trail: readonly PathCrumb[];
  /** What this page calls the thing the trail describes, for screen readers. */
  readonly label?: string;
  /** Controls that belong beside the path — a view toggle, an edit switch. */
  readonly children?: ReactNode;
}

export function PathBar({ trail, label = "Breadcrumb", children }: PathBarProps) {
  if (trail.length === 0) {
    return null;
  }
  const last = trail.length - 1;

  return (
    <div className="row path-bar">
      <nav className="crumbs" aria-label={label}>
        {trail.map((crumb, position) => (
          <span key={crumb.key} className="row-tight">
            {position > 0 && (
              <span className="sep" aria-hidden="true">
                /
              </span>
            )}
            <Crumb crumb={crumb} current={position === last} />
          </span>
        ))}
      </nav>
      {children !== undefined && (
        <>
          <span className="spacer" />
          {children}
        </>
      )}
    </div>
  );
}

function Crumb({ crumb, current }: { crumb: PathCrumb; current: boolean }) {
  // The current page is a destination nobody needs to be offered, and offering
  // it is how a breadcrumb ends up with a link that reloads the page you are on.
  if (current) {
    return (
      <span className="here" aria-current="page">
        {crumb.label}
      </span>
    );
  }
  if (crumb.onSelect !== undefined) {
    return (
      <button type="button" className="crumb" onClick={crumb.onSelect}>
        {crumb.label}
      </button>
    );
  }
  if (crumb.to !== undefined) {
    return (
      <Link className="crumb" to={crumb.to}>
        {crumb.label}
      </Link>
    );
  }
  // A crumb with no way to reach it: still shown, because dropping it would
  // silently shorten the path and misstate how deep this thing sits.
  return <span className="crumb-flat">{crumb.label}</span>;
}
