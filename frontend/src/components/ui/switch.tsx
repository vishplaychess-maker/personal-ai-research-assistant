import * as React from "react";
import { cn } from "../../lib/utils";

export interface SwitchProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onChange"> {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}

const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  ({ checked, onCheckedChange, className, disabled, ...props }, ref) => (
    <button
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full border transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        checked ? "bg-primary" : "bg-muted",
        disabled && "cursor-not-allowed opacity-50",
        className
      )}
      {...props}
    >
      <span
        className={cn(
          "absolute top-0.5 h-4 w-4 rounded-full bg-background shadow-sm transition-all",
          checked ? "left-[22px]" : "left-0.5"
        )}
      />
    </button>
  )
);
Switch.displayName = "Switch";

export { Switch };
