import { Link, useLocation } from "react-router-dom";

import { Notice } from "../components/Feedback";

export function NotFoundScreen() {
  const { pathname } = useLocation();
  return (
    <Notice kind="warn" title="No such screen">
      <p style={{ margin: 0 }}>
        <span className="mono">{pathname}</span> is not part of this app.
      </p>
      <p style={{ margin: 0 }}>
        <Link to="/search">Search</Link> · <Link to="/tree">Storage</Link> ·{" "}
        <Link to="/scan">Scan</Link>
      </p>
    </Notice>
  );
}
