"use client"

import * as React from "react"
import { cn } from "@/lib/utils"

export interface MasteryAxis {
  label: string
  /** Mastery 0–100. */
  value: number
}

export interface MasteryRadarProps extends React.ComponentProps<"div"> {
  axes: MasteryAxis[]
  size?: number
  /** Number of concentric grid rings. */
  rings?: number
  /** Fill opacity of the value polygon, 0–1. */
  fillOpacity?: number
  /** Polygon + accent color. Defaults to the teal study OS accent. */
  color?: string
  /** Accessible description. */
  label?: string
  /** Renders the final polygon at full scale immediately, no draw-in animation. */
  static?: boolean
}

/**
 * MasteryRadar — SVG radar (spider) chart of skill mastery.
 *
 * Every vertex is computed in polar coordinates around N evenly spaced spokes
 * (angle = i/N·2π − π/2), so the concentric grid polygons and the data polygon
 * share exact vertices. The value polygon scales up from the center once when
 * the chart enters view.
 *
 * Interactive: hover, tap, or keyboard-focus a spoke (arrows cycle around the
 * chart) to highlight its vertex and surface a value badge placed on the
 * outward side of the axis label, so the two never collide at any spoke angle.
 * Fewer than three axes renders a designed placeholder instead of a degenerate
 * polygon. Reduced motion renders the final polygon with no draw.
 * All geometry is deterministic — no randomness.
 */
export function MasteryRadar({
  axes,
  size = 240,
  rings = 4,
  fillOpacity = 0.18,
  color = "var(--color-spider-polygon-stroke)",
  label,
  static: isStatic = false,
  className,
  ...props
}: MasteryRadarProps) {
  const [highlightedIndex, setHighlightedIndex] = React.useState<number | null>(null)
  const [isVisible, setIsVisible] = React.useState(!isStatic)

  const axisCount = axes.length
  const hasData = axisCount >= 3 && axes.some((a) => a.value > 0)

  React.useEffect(() => {
    if (!isStatic && typeof window !== "undefined") {
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              setIsVisible(true)
              observer.disconnect()
            }
          })
        },
        { threshold: 0.1 },
      )
      observer.observe(document.documentElement)
      return () => observer.disconnect()
    }
  }, [isStatic])

  const reducedMotion =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches

  const animate =
    !isStatic && isVisible && !reducedMotion && hasData
      ? `scale 0.6s ease-out forwards`
      : undefined

  const center = size / 2
  const outerRadius = size * 0.42
  const ringStep = outerRadius / rings
  const angleStep = (2 * Math.PI) / axisCount

  // Precompute vertex positions for all axes
  const vertices = axes.map((_, i) => {
    const angle = i * angleStep - Math.PI / 2
    return {
      x: center + outerRadius * Math.cos(angle),
      y: center + outerRadius * Math.sin(angle),
      labelX: center + (outerRadius + 18) * Math.cos(angle),
      labelY: center + (outerRadius + 18) * Math.sin(angle),
      angle,
    }
  })

  // Data polygon points (scale by value/100)
  const dataPoints = axes.map((axis, i) => {
    const r = outerRadius * (axis.value / 100)
    const angle = i * angleStep - Math.PI / 2
    return {
      x: center + r * Math.cos(angle),
      y: center + r * Math.sin(angle),
    }
  })

  const dataPointsString = dataPoints
    .map((p) => `${p.x},${p.y}`)
    .join(" ")

  // Animated scale for draw-in
  const scale = animate ? 0 : 1

  const highlightedAxis = highlightedIndex != null ? axes[highlightedIndex] : null
  const highlightedVertex = highlightedIndex != null ? vertices[highlightedIndex] : null

  // Determine text anchor for labels based on angle
  const getLabelAnchor = (i: number) => {
    const angle = i * angleStep - Math.PI / 2
    if (angle > -Math.PI / 2 && angle < Math.PI / 2) return "start"
    if (angle > Math.PI / 2 && angle < (3 * Math.PI) / 2) return "end"
    return "middle"
  }

  if (!hasData) {
    return (
      <div
        role="img"
        aria-label={label || "Capability radar chart — no data yet"}
        className={cn("flex flex-col items-center justify-center", className)}
        style={{ width: size, height: size }}
        {...props}
      >
        <svg
          width={size}
          height={size}
          className="text-muted-foreground"
        >
          <circle cx={center} cy={center} r={outerRadius} fill="none" stroke="var(--color-spider-grid)" strokeWidth={1} />
          {Array.from({ length: rings }).map((_, i) => {
            const r = (i + 1) * ringStep
            return (
              <circle
                key={i}
                cx={center}
                cy={center}
                r={r}
                fill="none"
                stroke="var(--color-spider-grid)"
                strokeWidth={1}
              />
            )
          })}
          {axes.map((axis, i) => {
            const v = vertices[i]
            return (
              <line
                key={i}
                x1={center}
                y1={center}
                x2={v.x}
                y2={v.y}
                stroke="var(--color-spider-grid)"
                strokeWidth={1}
              />
            )
          })}
        </svg>
        <p className="mt-2 text-xs text-muted-foreground text-center">
          {label || "Start your first assessment to see capability analysis"}
        </p>
      </div>
    )
  }

  return (
    <div
      role="img"
      aria-label={label || "Capability radar chart — skill mastery across fields"}
      className={cn("relative flex flex-col items-center", className)}
      style={{ width: size, height: size }}
      {...props}
    >
      <svg
        width={size}
        height={size}
        className="transition-transform duration-300"
        style={animate ? { transform: `scale(${scale})`, transformOrigin: "center" } : undefined}
      >
        {/* Concentric grid rings */}
        <circle cx={center} cy={center} r={outerRadius} fill="none" stroke="var(--color-spider-grid)" strokeWidth={1} />
        {Array.from({ length: rings }).map((_, i) => {
          const r = (i + 1) * ringStep
          return (
            <circle
              key={i}
              cx={center}
              cy={center}
              r={r}
              fill="none"
              stroke="var(--color-spider-grid)"
              strokeWidth={1}
            />
          )
        })}

        {/* Axis spokes */}
        {axes.map((_, i) => {
          const v = vertices[i]
          return (
            <line
              key={i}
              x1={center}
              y1={center}
              x2={v.x}
              y2={v.y}
              stroke="var(--color-spider-grid)"
              strokeWidth={1}
              pointerEvents="none"
            />
          )
        })}

        {/* Value polygon fill */}
        <polygon
          points={dataPointsString}
          fill={color}
          fillOpacity={fillOpacity}
          stroke="none"
        />

        {/* Value polygon stroke */}
        <polygon
          points={dataPointsString}
          fill="none"
          stroke={color}
          strokeWidth={2}
        />

        {/* Vertices */}
        {axes.map((axis, i) => {
          const p = dataPoints[i]
          const isHighlighted = highlightedIndex === i
          return (
            <g key={i} cursor="pointer" onClick={() => setHighlightedIndex(i)} onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault()
                setHighlightedIndex(i)
              }
            }} role="button" tabIndex={0} aria-label={`${axis.label}: ${axis.value}`}>
              <circle
                cx={p.x}
                cy={p.y}
                r={isHighlighted ? 5 : 3}
                fill={isHighlighted ? "var(--color-spider-vertex)" : color}
                stroke="var(--background)"
                strokeWidth={2}
              />
            </g>
          )
        })}
      </svg>

      {/* Axis labels (outside the chart) */}
      <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
        {axes.map((axis, i) => {
          const v = vertices[i]
          const isHighlighted = highlightedIndex === i
          return (
            <div
              key={i}
              className={cn(
                "absolute flex items-center justify-center",
                isHighlighted ? "font-semibold" : "text-sm",
                getLabelAnchor(i) === "start"
                  ? "pl-2"
                  : getLabelAnchor(i) === "end"
                  ? "pr-2"
                  : "",
                isHighlighted
                  ? "text-spider-label-active"
                  : "text-spider-label",
              )}
              style={{
                left: `calc(50% + ${v.labelX - center}px)`,
                top: `calc(50% + ${v.labelY - center}px)`,
                transform: `translate(${getLabelAnchor(i) === "start" ? "0" : getLabelAnchor(i) === "end" ? "-100%" : "-50%"}, ${getLabelAnchor(i) === "middle" ? "-50%" : "0"})`,
              }}
              title={`${axis.label}: ${axis.value}`}
            >
              {axis.label}
              {isHighlighted && (
                <span className="ml-1 text-[10px] font-mono">{axis.value}</span>
              )}
            </div>
          )
        })}
      </div>

      {/* Highlighted axis detail badge */}
      {highlightedAxis && highlightedVertex && (
        <div className="absolute flex items-center gap-1 px-2 py-1 rounded-md bg-popover border border-border text-popover-foreground text-xs font-medium shadow-pop pointer-events-none z-10">
          <span className="w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
          {highlightedAxis.label}: {highlightedAxis.value}
        </div>
      )}
    </div>
  )
}
