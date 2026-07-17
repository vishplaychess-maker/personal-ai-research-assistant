/**
 * App smoke test — verifies the top-level component renders without crashing.
 *
 * Phase 5C — Search and Frontend Refactoring
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";

// JSDOM does not implement scrollIntoView
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe("App", () => {
  it("renders without crashing", () => {
    const { container } = render(<App />);
    expect(container).toBeTruthy();
  });

  it("renders the sidebar with Research Sessions title", () => {
    render(<App />);
    expect(screen.getByText("Research Sessions")).toBeTruthy();
  });

  it("renders the chat area with select session prompt", () => {
    render(<App />);
    expect(screen.getByText("Select or create a session")).toBeTruthy();
  });

  it("renders health indicators", () => {
    render(<App />);
    expect(screen.getByText("API")).toBeTruthy();
    expect(screen.getByText("LLM")).toBeTruthy();
  });

  it("renders the new session button", () => {
    render(<App />);
    expect(screen.getByText("✚ New")).toBeTruthy();
  });
});
