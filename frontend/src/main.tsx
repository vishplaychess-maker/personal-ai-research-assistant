import React from "react";
import ReactDOM from "react-dom/client";
import { AuthProvider } from "./AuthContext";
import App from "./App";
import { removeLegacyRefreshToken } from "./auth";
import "./index.css";

// Phase 7C migration: the refresh token now lives ONLY in an HttpOnly cookie.
// Remove any Phase 7B localStorage refresh token during startup (never sent
// to the backend). Users without a valid cookie log in once after migrating.
removeLegacyRefreshToken();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </React.StrictMode>,
);
