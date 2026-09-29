import { LOGO_PATH, LOGO_VIEWBOX } from "./logo-path";

export function Logo({ height = 24, className }: { height?: number; className?: string }) {
  return (
    <svg
      className={className}
      viewBox={LOGO_VIEWBOX}
      height={height}
      style={{ width: "auto" }}
      role="img"
      aria-label="Kronto"
    >
      <path fill="currentColor" d={LOGO_PATH} />
    </svg>
  );
}

/** Знак окна из лендинга: кольцо с синей точкой. */
export function WindowMark() {
  return (
    <span
      aria-hidden
      style={{
        width: 16,
        height: 16,
        borderRadius: "50%",
        border: "4px solid var(--ink)",
        background: "radial-gradient(circle, var(--accent) 0 2.5px, transparent 3px)",
        flex: "none",
        display: "inline-block",
      }}
    />
  );
}
