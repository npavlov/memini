import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app";
import { BackendProvider } from "./ipc/backend-context";
import { createTauriBackend } from "./ipc/tauri-backend";
// The library's styles: the tokens first, then the fonts and the components that use them.
import "./components/tokens.css";
import "./components/fonts.css";
import "./components/components.css";

const root = document.getElementById("root");
if (root !== null) {
  createRoot(root).render(
    <StrictMode>
      <BackendProvider backend={createTauriBackend()}>
        <App />
      </BackendProvider>
    </StrictMode>,
  );
}
