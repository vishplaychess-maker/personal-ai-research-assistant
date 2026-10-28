import type { ReactNode } from "react";
import { cn } from "../lib/utils";

/**
 * Three-column application shell.
 *
 * Desktop: [sidebar 260px] [centre] [inspector 400px].
 * Below `lg` (sidebar) / `xl` (inspector) each panel becomes an overlay drawer.
 *
 * Each panel is rendered exactly once: rendering it as both a desktop column
 * and a mobile overlay would put two copies of its landmark and of every
 * control in the DOM at the same time.
 *
 * Wrappers are plain divs on purpose: `Sidebar` and `Inspector` each render
 * their own `<aside>`.
 */
export interface AppShellProps {
  sidebar: ReactNode;
  header: ReactNode;
  inspector: ReactNode;
  children: ReactNode;
  sidebarOpen: boolean;
  inspectorOpen: boolean;
  onCloseSidebar: () => void;
  onCloseInspector: () => void;
}

export function AppShell({
  sidebar,
  header,
  inspector,
  children,
  sidebarOpen,
  inspectorOpen,
  onCloseSidebar,
  onCloseInspector,
}: AppShellProps) {
  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground">
      {/* Backdrops only on small screens, only while a drawer is open. */}
      {sidebarOpen ? (
        <button
          type="button"
          aria-label="Close sidebar"
          onClick={onCloseSidebar}
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
        />
      ) : null}
      {inspectorOpen ? (
        <button
          type="button"
          aria-label="Close inspector"
          onClick={onCloseInspector}
          className="fixed inset-0 z-30 bg-black/50 xl:hidden"
        />
      ) : null}

      <div
        aria-hidden={!sidebarOpen}
        className={cn(
          "z-40 shrink-0 overflow-hidden border-r border-border bg-background",
          "transition-[width] duration-200",
          sidebarOpen
            ? "w-[260px] max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:w-[280px] max-lg:max-w-[85vw] max-lg:shadow-2xl"
            : "hidden w-0 lg:border-r-0"
        )}
      >
        {sidebar}
      </div>

      <div className="relative flex h-full min-w-0 flex-1 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
      </div>

      <div
        aria-hidden={!inspectorOpen}
        className={cn(
          "z-40 shrink-0 overflow-hidden border-l border-border bg-background",
          "transition-[width] duration-200",
          inspectorOpen
            ? "w-[400px] max-xl:fixed max-xl:inset-y-0 max-xl:right-0 max-xl:w-[380px] max-xl:max-w-[90vw] max-xl:shadow-2xl"
            : "hidden w-0 xl:border-l-0"
        )}
      >
        {inspector}
      </div>
    </div>
  );
}
