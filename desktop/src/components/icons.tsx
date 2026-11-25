import type { ReactElement } from "react";

// The glyph's drawing is read from the brand source when the app is built, so the sidebar always
// shows the mark as it is in brand-src/, and that file is the one place to change it.
import glyphSource from "../../brand-src/glyph.svg?raw";

/** How one shape of the glyph is painted. An attribute the source leaves out is left out here. */
interface Paint {
  readonly fill: string | undefined;
  readonly stroke: string | undefined;
  readonly strokeWidth: string | undefined;
  readonly strokeLinecap: "round" | "butt" | "square" | undefined;
  readonly strokeLinejoin: "round" | "miter" | "bevel" | undefined;
}

export interface GlyphDrawing {
  /** The line of the mark: its path data and its paint. */
  readonly path: string;
  readonly paint: Paint;
  /** The point the line turns to. */
  readonly dotX: string;
  readonly dotY: string;
  readonly dotRadius: string;
  readonly dotFill: string | undefined;
}

/** The value of an attribute of the first element of a kind in an SVG source. */
function attributeOf(source: string, element: string, name: string): string | undefined {
  const tag = new RegExp(`<${element}\\b[^>]*>`, "u").exec(source)?.[0] ?? "";
  return new RegExp(`\\s${name}="([^"]*)"`, "u").exec(tag)?.[1];
}

function oneOf<T extends string>(value: string | undefined, allowed: readonly T[]): T | undefined {
  return allowed.find((item) => item === value);
}

/** The line and the point of a glyph source: its one `path` and its one `circle`. */
export function readGlyphDrawing(source: string): GlyphDrawing {
  return {
    path: attributeOf(source, "path", "d") ?? "",
    paint: {
      fill: attributeOf(source, "path", "fill"),
      stroke: attributeOf(source, "path", "stroke"),
      strokeWidth: attributeOf(source, "path", "stroke-width"),
      strokeLinecap: oneOf(attributeOf(source, "path", "stroke-linecap"), ["round", "butt", "square"]),
      strokeLinejoin: oneOf(attributeOf(source, "path", "stroke-linejoin"), ["round", "miter", "bevel"]),
    },
    dotX: attributeOf(source, "circle", "cx") ?? "0",
    dotY: attributeOf(source, "circle", "cy") ?? "0",
    dotRadius: attributeOf(source, "circle", "r") ?? "0",
    dotFill: attributeOf(source, "circle", "fill"),
  };
}

const DRAWING = readGlyphDrawing(glyphSource);

export interface GlyphProps {
  /** The side of the square the glyph is drawn in, in pixels. */
  readonly size?: number;
}

/**
 * The Meander glyph: one rounded line that turns inward to a point, in the colour of the text
 * around it. It decorates the name next to it, so it carries no name of its own.
 */
export function Glyph({ size = 24 }: GlyphProps): ReactElement {
  return (
    <svg className="glyph" viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
      <path d={DRAWING.path} {...DRAWING.paint} />
      <circle fill={DRAWING.dotFill} cx={DRAWING.dotX} cy={DRAWING.dotY} r={DRAWING.dotRadius} />
    </svg>
  );
}

export type IconName = "refresh" | "chevron-up" | "chevron-down";

const PATHS: Readonly<Record<IconName, string>> = {
  refresh: "M20 12a8 8 0 1 1-2.4-5.7 M20 4v4.5h-4.5",
  "chevron-up": "M6 15l6-6 6 6",
  "chevron-down": "M6 9l6 6 6-6",
};

export interface IconProps {
  readonly name: IconName;
}

/** A small line icon in the colour of its text. It is decoration: the control it is in names it. */
export function Icon({ name }: IconProps): ReactElement {
  return (
    <svg className="icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
      <path fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" d={PATHS[name]} />
    </svg>
  );
}
