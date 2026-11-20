/** Collision-free label placement. Labels move; the data points they describe never do. */
export interface LabelIn {
  id: string;
  /** Screen position of the point being labelled. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** Which side of the point the label sits on. */
  side: "left" | "right" | "center";
  /** Preferred vertical offset from the point (e.g. -16 to sit above it). */
  dy?: number;
}
export interface LabelOut extends LabelIn { left: number; top: number; leader: boolean }

const overlaps = (a: { left: number; top: number; w: number; h: number }, b: { left: number; top: number; w: number; h: number }, gap: number) =>
  a.left < b.left + b.w + gap && b.left < a.left + a.w + gap && a.top < b.top + b.h + gap && b.top < a.top + a.h + gap;

/**
 * Place labels inside ``box`` so none overlap and none is clipped. Each label is tried at its preferred spot, then nudged up and
 * down in label-height steps. ``leader`` is true when a label ended up away from its point so a leader line should be drawn.
 */
export function resolveLabels(items: LabelIn[], box: { width: number; height: number }, gap = 4): LabelOut[] {
  const placed: LabelOut[] = [];
  const clampX = (l: number, w: number) => Math.max(4, Math.min(l, box.width - 4 - w));
  const clampY = (t: number, h: number) => Math.max(4, Math.min(t, box.height - 4 - h));
  for (const it of [...items].sort((p, q) => p.y + (p.dy ?? 0) - (q.y + (q.dy ?? 0)))) {
    const wanted = it.side === "right" ? it.x - it.w - 10 : it.side === "left" ? it.x + 10 : it.x - it.w / 2;
    const left = clampX(wanted, it.w);
    const base = it.y - it.h / 2 + (it.dy ?? 0);
    const preferredTop = clampY(base, it.h);
    let best: number | null = null;
    for (let k = 0; k <= 16 && best === null; k++) {
      for (const sign of k === 0 ? [0] : [1, -1]) {
        const top = clampY(base + sign * k * (it.h + gap), it.h);
        if (!placed.some((p) => overlaps({ left, top, w: it.w, h: it.h }, p, gap))) { best = top; break; }
      }
    }
    const top = best ?? clampY(base, it.h);
    // A leader line is needed only when collision handling moved the label away from where it was asked to sit.
    placed.push({ ...it, left, top, leader: Math.abs(top - preferredTop) > 6 || Math.abs(left - wanted) > 6 });
  }
  return items.map((it) => placed.find((p) => p.id === it.id)!);
}
