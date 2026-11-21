import { useCallback, useLayoutEffect, useRef } from "react";
import { selectionMotion, type SelectionPose } from "./selectionMotion";

type RunningSelection = { animation: Animation; sample: (time: number) => SelectionPose };

function sharesEdge(from: DOMRect, to: DOMRect, track: CSSStyleDeclaration): boolean {
  const horizontalGap = Math.max(from.left, to.left) - Math.min(from.right, to.right);
  const verticalGap = Math.max(from.top, to.top) - Math.min(from.bottom, to.bottom);
  const withinGap = (distance: number, gap: string) =>
    distance >= -1 && distance <= (parseFloat(gap) || 0) + 1;

  return (
    withinGap(horizontalGap, track.columnGap) && -verticalGap >= Math.min(from.height, to.height) * 0.75
  ) || (
    withinGap(verticalGap, track.rowGap) && -horizontalGap >= Math.min(from.width, to.width) * 0.75
  );
}

export function useSelectionSlide(value: string) {
  const thumbNode = useRef<HTMLSpanElement | null>(null);
  const previousOption = useRef<HTMLElement | null>(null);
  const interruptedPose = useRef<SelectionPose | null>(null);
  const running = useRef<RunningSelection | null>(null);

  const indicator = useCallback((node: HTMLSpanElement | null) => {
    const motion = running.current;
    const time = motion?.animation.currentTime;
    if (node === null && motion && typeof time === "number") {
      interruptedPose.current = motion.sample(time);
    }
    thumbNode.current = node;
  }, []);

  useLayoutEffect(() => {
    const thumb = thumbNode.current;
    const next = thumb?.parentElement ?? null;
    const previous = previousOption.current;
    const interrupted = interruptedPose.current;
    previousOption.current = next;
    interruptedPose.current = null;

    const track = next?.parentElement;
    if (!thumb || !next || !previous || !track || previous === next ||
        previous.parentElement !== track || !previous.isConnected ||
        typeof thumb.animate !== "function" || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }

    const from = previous.getBoundingClientRect();
    const to = next.getBoundingClientRect();
    const trackStyle = getComputedStyle(track);
    if (!from.width || !from.height || !to.width || !to.height || !sharesEdge(from, to, trackStyle)) return;

    // The interrupted pose is relative to its own target, which is now the previous option.
    const start: SelectionPose = {
      x: {
        value: from.left - to.left + (from.width - to.width) / 2 + (interrupted?.x.value ?? 0),
        velocity: interrupted?.x.velocity ?? 0
      },
      y: {
        value: from.top - to.top + (from.height - to.height) / 2 + (interrupted?.y.value ?? 0),
        velocity: interrupted?.y.velocity ?? 0
      },
      width: interrupted?.width ?? { value: from.width, velocity: 0 },
      height: interrupted?.height ?? { value: from.height, velocity: 0 }
    };
    const glass = trackStyle.getPropertyValue("--selection-glass-motion").trim() === "1";
    const motion = selectionMotion(start, to, glass);
    const timing = { duration: motion.duration };
    const animation = thumb.animate(motion.body, timing);
    const light = glass ? thumb.querySelector(".selection-rim-glint") : null;
    if (light) thumb.setAttribute("data-selection-moving", "");
    const reflection = light?.animate(motion.reflection, timing);

    running.current = { animation, sample: motion.sample };
    const clearMotion = () => {
      reflection?.cancel();
      thumb.removeAttribute("data-selection-moving");
      running.current = null;
    };
    animation.onfinish = clearMotion;
    return () => {
      animation.onfinish = null;
      animation.cancel();
      clearMotion();
    };
  }, [value]);

  return indicator;
}
