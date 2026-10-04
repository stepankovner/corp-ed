import { LOGO_PATH, LOGO_VIEWBOX } from "./logo-path";

export function Logo({ height = 24, className }: { height?: number; className?: string }) {
  return (
    <svg
      className={className}
      viewBox={LOGO_VIEWBOX}
      height={height}
      style={{ width: "auto" }}
      role="img"
      aria-label="kronto"
    >
      <path fill="currentColor" d={LOGO_PATH} />
    </svg>
  );
}

/** Знак окна из лендинга: кольцо с точкой акцентного цвета. */
export function WindowMark({ size = 16 }: { size?: number }) {
  const dot = size * 0.16;
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        border: `${size / 4}px solid var(--ink)`,
        background: `radial-gradient(circle, var(--accent) 0 ${dot}px, transparent ${dot + 0.5}px)`,
        flex: "none",
        display: "inline-block",
      }}
    />
  );
}
