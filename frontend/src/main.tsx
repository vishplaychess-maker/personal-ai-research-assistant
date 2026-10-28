import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "./components/ThemeProvider";
import { AuthProvider } from "./AuthContext";
import App from "./App";
import ShareCard from "./ShareCard";
import { removeLegacyRefreshToken } from "./auth";
import "./index.css";

// Phase 7C migration: the refresh token now lives ONLY in an HttpOnly cookie.
removeLegacyRefreshToken();

// Public share page: /share/agents/{share_id} is rendered standalone, BEFORE
// the auth gate, so anyone with the link can view it without logging in.
const shareMatch = window.location.pathname.match(/^\/share\/agents\/([^/]+)$/);

if (shareMatch) {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <ThemeProvider>
        <ShareCard shareId={decodeURIComponent(shareMatch[1])} />
      </ThemeProvider>
    </React.StrictMode>,
  );
} else {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <ThemeProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ThemeProvider>
    </React.StrictMode>,
  );
}
