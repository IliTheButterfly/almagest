import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("#root is missing from index.html");
}

createRoot(root).render(
  <StrictMode>
    {/*
     * A history router, not a hash router: the paths are the backend's
     * `/s/{short_id}` redirect targets, so `/locations/7` has to be a real URL the
     * server can hand to the SPA. The API serves `index.html` for unknown paths in
     * production and Vite does the same in development.
     */}
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
