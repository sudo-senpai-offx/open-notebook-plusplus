"use client"

import * as React from "react"
import { cn } from "@/lib/utils"

interface AvatarProps extends React.HTMLAttributes<HTMLDivElement> {
  src?: string
  alt?: string
  size?: "sm" | "md" | "lg"
}

const sizeStyles: Record<string, string> = {
  sm: "w-8 h-8",
  md: "w-10 h-10",
  lg: "w-12 h-12",
}

export function Avatar({
  className,
  src,
  alt,
  size = "md",
  ...props
}: AvatarProps) {
  return (
    <div
      className={cn(
        "relative shrink-0 overflow-hidden rounded-full bg-border flex items-center justify-center",
        sizeStyles[size],
        className,
      )}
      {...props}
    >
      {src ? (
        <img
          src={src}
          alt={alt || "Avatar"}
          className="aspect-square h-full w-full object-cover"
        />
      ) : (
        <div className="text-muted-foreground text-sm font-medium">
          {alt ? alt.charAt(0).toUpperCase() : "?"}
        </div>
      )}
    </div>
  )
}
