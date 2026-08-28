import * as React from "react";
import { cn } from "../../lib/utils";

interface TabsContextValue {
  value: string;
  onValueChange: (value: string) => void;
}

const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabsContext() {
  const context = React.useContext(TabsContext);
  if (!context) {
    throw new Error("Tabs components must be used within Tabs");
  }
  return context;
}

interface TabsProps {
  value: string;
  onValueChange: (value: string) => void;
  children: React.ReactNode;
  className?: string;
  defaultValue?: string;
}

export function Tabs({ value, onValueChange, children, className, defaultValue }: TabsProps) {
  const [activeValue, setActiveValue] = React.useState(defaultValue || value);
  
  const handleValueChange = (newValue: string) => {
    setActiveValue(newValue);
    onValueChange(newValue);
  };

  return (
    <TabsContext.Provider value={{ value: activeValue, onValueChange: handleValueChange }}>
      <div className={cn("w-full", className)}>
        {children}
      </div>
    </TabsContext.Provider>
  );
}

interface TabsListProps {
  children: React.ReactNode;
  className?: string;
}

export function TabsList({ children, className }: TabsListProps) {
  return (
    <div
      className={cn(
        "inline-flex h-9 items-center justify-center rounded-lg bg-white/3 p-1",
        className
      )}
      role="tablist"
    >
      {children}
    </div>
  );
}

interface TabsTriggerProps {
  value: string;
  children: React.ReactNode;
  disabled?: boolean;
  className?: string;
}

export function TabsTrigger({ value, children, disabled, className }: TabsTriggerProps) {
  const context = useTabsContext();
  const isActive = context.value === value;
  
  return (
    <button
      role="tab"
      aria-selected={isActive}
      aria-disabled={disabled}
      data-state={isActive ? "active" : "inactive"}
      disabled={disabled}
      onClick={() => !disabled && context.onValueChange(value)}
      className={cn(
        "flex items-center justify-center h-9 px-3 text-sm font-medium rounded-md transition-all",
        "data-[state=active]:bg-white/5 data-[state=active]:text-foreground",
        "hover:bg-white/3 focus:outline-none focus:ring-1 focus:ring-ring",
        "disabled:pointer-events-none disabled:opacity-50",
        className
      )}
    >
      {children}
    </button>
  );
}

interface TabsContentProps {
  value: string;
  children: React.ReactNode;
  className?: string;
}

export function TabsContent({ value, children, className }: TabsContentProps) {
  const context = useTabsContext();
  const isActive = context.value === value;
  
  if (!isActive) return null;
  
  return (
    <div
      role="tabpanel"
      className={cn(
        "mt-4 animate-in fade-in-0 duration-200",
        className
      )}
    >
      {children}
    </div>
  );
}