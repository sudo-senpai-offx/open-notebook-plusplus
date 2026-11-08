"use client"

import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const toggleVariants = cva(
  "inline-flex items-center justify-center cursor-pointer rounded-full border-2 transition-colors duration-200 ease-out disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "bg-surface border-border text-foreground data-[state=checked]:bg-primary data-[state=checked]:border-primary data-[state=unchecked]:bg-transparent",
        ghost:
          "bg-accent border-transparent text-accent-foreground data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground data-[state=unchecked]:bg-transparent",
      },
      toggleSize: {
        default: "w-10 h-6",
        sm: "w-8 h-4",
        lg: "w-12 h-7",
      },
    },
    defaultVariants: {
      variant: "default",
      toggleSize: "default",
    },
  }
)

const handleVariants = cva(
  "block rounded-full border-2 shadow-lift transition-transform duration-200 ease-out",
  {
    variants: {
      variant: {
        default:
          "bg-surface border-border data-[state=checked]:translate-x-full data-[state=checked]:bg-on-primary data-[state=checked]:border-on-primary",
        ghost:
          "bg-accent-foreground border-transparent data-[state=checked]:translate-x-full",
      },
      toggleSize: {
        default: "w-5 h-5",
        sm: "w-3 h-3",
        lg: "w-6 h-6",
      },
    },
    defaultVariants: {
      variant: "default",
      toggleSize: "default",
    },
  }
)

interface ToggleProps
  extends React.ComponentPropsWithRef<"input">,
    VariantProps<typeof toggleVariants> {
  variant?: VariantProps<typeof toggleVariants>["variant"]
  toggleSize?: VariantProps<typeof toggleVariants>["toggleSize"]
  checked?: boolean
  defaultChecked?: boolean
  onCheckedChange?: (checked: boolean) => void
  name?: string
  id?: string
  required?: boolean
  "aria-label"?: string
}

const Toggle = React.forwardRef<HTMLInputElement, ToggleProps>(
  function Toggle(
    { className, variant, toggleSize, checked, defaultChecked, onCheckedChange, ...props },
    ref,
  ) {
    const [isChecked, setIsChecked] = React.useState(defaultChecked ?? false)

    const isControlled = checked !== undefined
    const currentChecked = isControlled ? checked : isChecked

    const handleChange = (event: React.ChangeEvent<HTMLInputElement>) => {
      const value = event.target.checked
      if (!isControlled) {
        setIsChecked(value)
      }
      onCheckedChange?.(value)
    }

    return (
      <span
        className={cn(toggleVariants({ variant, toggleSize }), className)}
        role="switch"
        data-state={currentChecked ? "checked" : "unchecked"}
        aria-checked={currentChecked}
        {...props}
      >
        <Slot>
          <input
            type="checkbox"
            ref={ref as React.Ref<HTMLInputElement>}
            checked={currentChecked}
            onChange={handleChange}
            className="sr-only"
            data-state={currentChecked ? "checked" : "unchecked"}
          />
        </Slot>
        <span
          className={cn(handleVariants({ variant, toggleSize }))}
          aria-hidden="true"
        />
      </span>
    )
  },
)

Toggle.displayName = "Toggle"

export { Toggle, toggleVariants }
