import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app";
import { BackendProvider } from "./ipc/backend-context";
import { createTauriBackend } from "./ipc/tauri-backend";
import "./styles.css";

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
