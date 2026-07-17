/**
 * Phase 6C — Authentication screen with Login and Register forms.
 *
 * Features:
 *  - Login form with username and password
 *  - Registration form with username, email, and password
 *  - Client-side validation (username length, password length, email format)
 *  - Loading state during authentication
 *  - Generic error messages (no username/email enumeration)
 *  - Password visibility toggle
 *  - Switch between Login and Register modes
 *  - Auto-login after successful registration
 */

import { useState, type FormEvent } from "react";
import { useAuth } from "./AuthContext";
import "./AuthScreen.css";

export function AuthScreen() {
  const { login, register, authError, clearError, isLoading: authLoading } = useAuth();

  // Mode
  const [mode, setMode] = useState<"login" | "register">("login");
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

  // Form fields
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  // Validation errors (client-side only)
  const [validationError, setValidationError] = useState<string | null>(null);

  const switchMode = () => {
    setMode(mode === "login" ? "register" : "login");
    setValidationError(null);
    clearError();
    setPassword("");
    setConfirmPassword("");
  };

  const validate = (): boolean => {
    setValidationError(null);

    if (!username.trim()) {
      setValidationError("Username is required");
      return false;
    }
    if (username.trim().length < 3) {
      setValidationError("Username must be at least 3 characters");
      return false;
    }
    if (mode === "register") {
      if (!email.trim()) {
        setValidationError("Email is required");
        return false;
      }
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) {
        setValidationError("Please enter a valid email address");
        return false;
      }
    }
    if (!password) {
      setValidationError("Password is required");
      return false;
    }
    if (password.length < 8) {
      setValidationError("Password must be at least 8 characters");
      return false;
    }
    if (mode === "register" && password !== confirmPassword) {
      setValidationError("Passwords do not match");
      return false;
    }
    return true;
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    if (!validate()) return;

    setSubmitting(true);
    try {
      if (mode === "login") {
        await login(username.trim(), password);
      } else {
        await register(username.trim(), email.trim(), password);
      }
    } catch {
      // Error is set in context — clear form fields for security
      setPassword("");
      setConfirmPassword("");
    } finally {
      setSubmitting(false);
    }
  };

  const displayError = validationError || authError;
  const isLoading = authLoading || submitting;

  // ── Render ──────────────────────────────────────────────

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-header">
          <div className="auth-logo">🧠</div>
          <h1 className="auth-title">AI Research Assistant</h1>
          <p className="auth-subtitle">
            {mode === "login" ? "Sign in to your account" : "Create a new account"}
          </p>
        </div>

        <form className="auth-form" onSubmit={handleSubmit} noValidate>
          {/* Error display */}
          {displayError && (
            <div className="auth-error">
              <span className="auth-error-icon">⚠️</span>
              <span>{displayError}</span>
            </div>
          )}

          {/* Username */}
          <div className="auth-field">
            <label className="auth-label" htmlFor="auth-username">
              Username
            </label>
            <input
              id="auth-username"
              name="username"
              className="auth-input"
              type="text"
              placeholder="your username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={isLoading}
              autoComplete="username"
              autoFocus
            />
          </div>

          {/* Email (register only) */}
          {mode === "register" && (
            <div className="auth-field">
              <label className="auth-label" htmlFor="auth-email">
                Email
              </label>
              <input
                id="auth-email"
                name="email"
                className="auth-input"
                type="email"
                placeholder="your@email.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={isLoading}
                autoComplete="email"
              />
            </div>
          )}

          {/* Password */}
          <div className="auth-field">
            <label className="auth-label" htmlFor="auth-password">
              Password
            </label>
            <div className="auth-password-wrapper">
              <input
                id="auth-password"
                name="password"
                className="auth-input auth-password-input"
                type={showPassword ? "text" : "password"}
                placeholder={mode === "register" ? "at least 8 characters" : "your password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={isLoading}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
              />
              <button
                type="button"
                className="auth-password-toggle"
                onClick={() => setShowPassword(!showPassword)}
                tabIndex={-1}
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? "🙈" : "👁️"}
              </button>
            </div>
          </div>

          {/* Confirm password (register only) */}
          {mode === "register" && (
            <div className="auth-field">
              <label className="auth-label" htmlFor="auth-confirm-password">
                Confirm Password
              </label>
              <div className="auth-password-wrapper">
                <input
                  id="auth-confirm-password"
                  name="confirmPassword"
                  className="auth-input auth-password-input"
                  type={showConfirmPassword ? "text" : "password"}
                  placeholder="repeat your password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  disabled={isLoading}
                  autoComplete="new-password"
                />
                <button
                  type="button"
                  className="auth-password-toggle"
                  onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                  tabIndex={-1}
                  aria-label={showConfirmPassword ? "Hide password" : "Show password"}
                >
                  {showConfirmPassword ? "🙈" : "👁️"}
                </button>
              </div>
            </div>
          )}

          {/* Submit button */}
          <button type="submit" className="auth-submit" disabled={isLoading}>
            {isLoading ? (
              <span className="auth-submit-loading">
                <span className="auth-spinner" />
                {mode === "login" ? "Signing in…" : "Creating account…"}
              </span>
            ) : mode === "login" ? (
              "Sign In"
            ) : (
              "Create Account"
            )}
          </button>
        </form>

        {/* Switch mode */}
        <div className="auth-switch">
          <span className="auth-switch-text">
            {mode === "login" ? "Don't have an account?" : "Already have an account?"}
          </span>
          <button
            type="button"
            className="auth-switch-btn"
            onClick={switchMode}
            disabled={isLoading}
          >
            {mode === "login" ? "Create one" : "Sign in"}
          </button>
        </div>
      </div>
    </div>
  );
}
