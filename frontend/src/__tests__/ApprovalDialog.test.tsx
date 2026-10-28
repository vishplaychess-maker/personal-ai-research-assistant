/**
 * MP-3C3C — MCP tool-approval modal.
 *
 * Verifies: opens on an approval id, pulls the REDACTED detail from the
 * authenticated approval API (never reconstructs from marker text), explicit
 * one-shot Approve / Deny, safe handling of a stale / already-decided
 * approval, and that no sensitive envelope field is ever rendered.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ApprovalDialog from "../components/ApprovalDialog";
import type { ApprovalDetail } from "../types";

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
  // Radix relies on these in jsdom
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

const getApproval = vi.fn();
const approveRun = vi.fn();
const denyRun = vi.fn();
const getApprovalAction = vi.fn();
vi.mock("../api", () => ({
  API: {
    getApproval: (...a: unknown[]) => getApproval(...a),
    getApprovalAction: (...a: unknown[]) => getApprovalAction(...a),
    approveRun: (...a: unknown[]) => approveRun(...a),
    denyRun: (...a: unknown[]) => denyRun(...a),
  },
}));

const READY: ApprovalDetail = {
  id: "ap_1",
  state: "requested",
  tool_id: "fs.read_file",
  risk_level: "high",
  purpose_redacted: "read a file the assistant named",
  created_at: "2026-09-11T00:00:00Z",
};

beforeEach(() => {
  getApproval.mockReset().mockResolvedValue(READY);
  getApprovalAction.mockReset();
  approveRun.mockReset().mockResolvedValue({ id: "ap_1", state: "approved" });
  denyRun.mockReset().mockResolvedValue({ id: "ap_1", state: "denied" });
});

/** the explicit footer Close button (Radix also renders a decorative X) */
function footerClose(): HTMLElement {
  const btns = screen.getAllByRole("button", { name: /close/i });
  const el = btns.find((b) => b.textContent === "Close" && !b.querySelector("svg"));
  if (!el) throw new Error("footer Close button not found");
  return el;
}

function setup(props: Partial<React.ComponentProps<typeof ApprovalDialog>> = {}) {
  const onApprove = vi.fn();
  const onDeny = vi.fn();
  const onDismiss = vi.fn();
  render(
    <ApprovalDialog
      approvalId="ap_1"
      phase="waiting_approval"
      onApprove={onApprove}
      onDeny={onDeny}
      onDismiss={onDismiss}
      {...props}
    />,
  );
  return { onApprove, onDeny, onDismiss };
}

describe("ApprovalDialog", () => {
  it("opens, fetches redacted detail by id, and shows tool + risk + purpose", async () => {
    setup();
    await waitFor(() => expect(getApproval).toHaveBeenCalledWith("ap_1"));
    expect(screen.getByRole("dialog")).toHaveAccessibleName(/tool approval required/i);
    expect(await screen.findByText("fs.read_file")).toBeInTheDocument();
    // risk is spelled out, not colour-only
    expect(screen.getByText(/high risk/i)).toBeInTheDocument();
    expect(screen.getByText(/read a file the assistant named/i)).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("Approve calls approveRun exactly once even on a double click, then onApprove", async () => {
    const { onApprove } = setup();
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    fireEvent.click(approve);
    fireEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledOnce());
    expect(approveRun).toHaveBeenCalledExactlyOnceWith("ap_1");
    expect(denyRun).not.toHaveBeenCalled();
  });

  it("Deny calls denyRun once, then onDeny, and shows a terminal denied state", async () => {
    const { onDeny } = setup();
    fireEvent.click(await screen.findByRole("button", { name: /^deny$/i }));
    await waitFor(() => expect(onDeny).toHaveBeenCalledOnce());
    expect(denyRun).toHaveBeenCalledExactlyOnceWith("ap_1");
    expect(await screen.findByText(/was not run/i)).toBeInTheDocument();
  });

  it("a stale / already-decided approval is not actionable", async () => {
    getApproval.mockResolvedValue({ ...READY, state: "consumed" });
    const { onDismiss } = setup();
    await screen.findByText(/no longer available/i);
    expect(screen.queryByRole("button", { name: /^approve$/i })).not.toBeInTheDocument();
    fireEvent.click(footerClose());
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("surfaces a safe error if the detail fetch fails", async () => {
    getApproval.mockRejectedValue(new Error("gone"));
    setup();
    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    expect(footerClose()).toBeInTheDocument();
  });

  it("never renders sensitive envelope fields", async () => {
    getApproval.mockResolvedValue({
      ...READY,
      // fields the backend must never leak — belt-and-braces: even if present
      // on the object, the component must not paint them
      api_key: "sk-SECRET-KEY",
      action_hash: "HASH-DEADBEEF",
      session_family_id: "sf-PRIVATE",
      raw_args: { path: "/etc/shadow" },
    } as unknown as ApprovalDetail);
    const { container } = render(
      <ApprovalDialog
        approvalId="ap_1"
        phase="waiting_approval"
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    await screen.findByText("fs.read_file");
    const html = container.ownerDocument.body.innerHTML;
    expect(html).not.toContain("sk-SECRET-KEY");
    expect(html).not.toContain("HASH-DEADBEEF");
    expect(html).not.toContain("sf-PRIVATE");
    expect(html).not.toContain("/etc/shadow");
  });
});

// ── MP-4C informed approval: the EXACT Python action ─────────────────

const PYTHON_READY: ApprovalDetail = {
  id: "ap_py",
  state: "requested",
  tool_id: "python_sandbox",
  risk_level: "medium",
  purpose_redacted: "",
  action_hash: "ACTIONHASH-1",
  created_at: "2026-09-24T00:00:00Z",
};

const PYTHON_CODE = "print('<script>MP4C-EXACT</script>')";
const PYTHON_PREVIEW = {
  approval_id: "ap_py",
  tool_id: "python_sandbox",
  code: PYTHON_CODE,
  code_bytes: PYTHON_CODE.length,
  args_hash: "ARGSHASH-1",
  action_hash: "ACTIONHASH-1",
  purpose: "",
  expires_at: null,
};

describe("ApprovalDialog — exact Python action preview", () => {
  beforeEach(() => {
    getApprovalAction.mockReset().mockResolvedValue(PYTHON_PREVIEW);
  });

  it("keeps Approve disabled until the COMPLETE exact preview loads, then enables it", async () => {
    let resolve!: (v: unknown) => void;
    getApprovalAction.mockReturnValue(new Promise((r) => { resolve = r; }));
    getApproval.mockResolvedValue(PYTHON_READY);
    const { onApprove } = setup({ approvalId: "ap_py" });
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    expect(approve).toBeDisabled();
    resolve(PYTHON_PREVIEW);
    await waitFor(() => expect(approve).toBeEnabled());
    fireEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledOnce());
    expect(approveRun).toHaveBeenCalledWith("ap_py");
  });

  it("renders the exact code escaped, read-only, with a code-execution warning", async () => {
    getApproval.mockResolvedValue(PYTHON_READY);
    setup({ approvalId: "ap_py" });
    expect(await screen.findByText(/will run Python code exactly as shown/i)).toBeInTheDocument();
    const pre = await screen.findByLabelText(/exact python code/i);
    expect(pre.textContent).toContain("MP4C-EXACT");
    // escaped: the literal markup must never become a real element
    expect(document.querySelector("script")).toBeNull();
  });

  it("keeps Approve disabled with a bounded error when the preview fails; Deny stays available", async () => {
    getApproval.mockResolvedValue(PYTHON_READY);
    getApprovalAction.mockRejectedValue(new Error("gone"));
    setup({ approvalId: "ap_py" });
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    await waitFor(() => expect(approve).toBeDisabled());
    expect(await screen.findByText(/approval is disabled/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^deny$/i })).toBeEnabled();
  });

  it("keeps Approve disabled when the preview identity does not match the dialog", async () => {
    getApproval.mockResolvedValue(PYTHON_READY);
    getApprovalAction.mockResolvedValue({ ...PYTHON_PREVIEW, action_hash: "OTHER" });
    setup({ approvalId: "ap_py" });
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    await waitFor(() => expect(approve).toBeDisabled());
  });

  it("never fetches a preview for a non-python approval", async () => {
    setup();
    await screen.findByText("fs.read_file");
    expect(getApprovalAction).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^approve$/i })).toBeEnabled());
  });
});

// ── MP-4C decision 2: the EXACT memory/directive content to be saved ──

const SAVE_READY: ApprovalDetail = {
  id: "ap_mem",
  state: "requested",
  tool_id: "memory_write",
  risk_level: "medium",
  purpose_redacted: "",
  action_hash: "ACTIONHASH-M",
  created_at: "2026-09-24T00:00:00Z",
};

const SAVE_CONTENT = "the user prefers bullet points";
const SAVE_PREVIEW = {
  approval_id: "ap_mem",
  tool_id: "memory_write",
  kind: "memory" as const,
  content: SAVE_CONTENT,
  content_chars: SAVE_CONTENT.length,
  args_hash: "ARGSHASH-M",
  action_hash: "ACTIONHASH-M",
  purpose: "",
  expires_at: null,
};

describe("ApprovalDialog — exact memory/directive save preview", () => {
  beforeEach(() => {
    getApprovalAction.mockReset().mockResolvedValue(SAVE_PREVIEW);
  });

  it("renders the exact content and enables Approve once it loads", async () => {
    getApproval.mockResolvedValue(SAVE_READY);
    const { onApprove } = setup({ approvalId: "ap_mem" });
    expect(await screen.findByText(/exact memory shown below/i)).toBeInTheDocument();
    const pre = await screen.findByLabelText(/exact content to be saved/i);
    expect(pre.textContent).toContain(SAVE_CONTENT);
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    await waitFor(() => expect(approve).toBeEnabled());
    fireEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledOnce());
    expect(approveRun).toHaveBeenCalledWith("ap_mem");
  });

  it("keeps Approve disabled with a bounded error when the save preview fails", async () => {
    getApproval.mockResolvedValue(SAVE_READY);
    getApprovalAction.mockRejectedValue(new Error("gone"));
    setup({ approvalId: "ap_mem" });
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    await waitFor(() => expect(approve).toBeDisabled());
    expect(await screen.findByText(/exact content could not be loaded/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^deny$/i })).toBeEnabled();
  });

  it("keeps Approve disabled when the save preview hash does not match", async () => {
    getApproval.mockResolvedValue(SAVE_READY);
    getApprovalAction.mockResolvedValue({ ...SAVE_PREVIEW, action_hash: "OTHER" });
    setup({ approvalId: "ap_mem" });
    const approve = await screen.findByRole("button", { name: /^approve$/i });
    await waitFor(() => expect(approve).toBeDisabled());
  });
});
