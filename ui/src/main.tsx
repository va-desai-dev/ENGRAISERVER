import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/app.css";
import { App } from "./App";

const host = document.getElementById("root");
if (!host) throw new Error("#root is missing from the document");

createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
