import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "./components/ThemeProvider";
import { AuthProvider } from "./AuthContext";
import App from "./App";
import { removeLegacyRefreshToken } from "./auth";
import "./index.css";

// Phase 7C migration: the refresh token now lives ONLY in an HttpOnly cookie.
removeLegacyRefreshToken();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <AuthProvider>
        <App />
      </AuthProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
