"use client"

import * as React from "react"
import { cn } from "@/lib/utils"

interface ProgressRingProps {
  /** Progress percentage (0-100) */
  value: number
  /** Size of the ring in pixels */
  size?: number
  /** Stroke width */
  strokeWidth?: number
  /** Show percentage text inside */
  showLabel?: boolean
  /** Custom label text */
  label?: string
  className?: string
}

export function ProgressRing({
  value,
  size = 40,
  strokeWidth = 4,
  showLabel = false,
  label,
  className,
}: ProgressRingProps) {
  const radius = (size - strokeWidth * 2) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (value / 100) * circumference
  const clampedValue = Math.max(0, Math.min(100, value))
  const displayLabel = label ?? `${Math.round(clampedValue)}%`

  return (
    <div
      className={cn("relative flex items-center justify-center", className)}
      style={{ width: size, height: size }}
    >
      <svg
        width={size}
        height={size}
        className="transform -rotate-90"
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--color-progress-ring-bg)"
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--color-progress-ring-fill)"
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-500 ease-out"
        />
      </svg>
      {showLabel && (
        <span
          className="absolute text-[10px] font-medium text-muted-foreground"
        >
          {displayLabel}
        </span>
      )}
    </div>
  )
}
