// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import type { CSSProperties } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useSelectionSlide } from "./useSelectionSlide";

const positions: Record<string, [number, number]> = {
  a: [0, 0],
  b: [84, 0],
  c: [168, 0],
  d: [0, 44],
  e: [84, 44],
  outside: [84, 88]
};

function Options({ selected }: { selected: string }) {
  const indicator = useSelectionSlide(selected);
  return [["a", "b", "c", "d", "e"], ["outside"]].map((group) => (
    <div key={group[0]} style={{ rowGap: 4, columnGap: 4, "--selection-glass-motion": "1" } as CSSProperties}>
      {group.map((option) => (
        <button data-option={option} key={option} type="button">
          {option}
          {selected === option ? <span ref={indicator} /> : null}
        </button>
      ))}
    </div>
  ));
}

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(HTMLElement.prototype, "animate");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mockMotion() {
  const animate = vi.fn<(frames: Keyframe[], timing: KeyframeAnimationOptions) => { cancel: () => void; currentTime: number }>(
    () => ({ cancel: vi.fn(), currentTime: 0 })
  );
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  const media = { matches: false };
  vi.stubGlobal("matchMedia", () => media);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const position = positions[this.dataset.option ?? ""];
    return position ? new DOMRect(...position, 80, 40) : new DOMRect();
  });
  return { animate, media };
}

function transform(frame: Keyframe) {
  const [, x, y, scaleX, scaleY] = String(frame.transform).match(/^translate\(([^,]+)px, ([^,]+)px\) scale\(([^,]+), ([^)]+)\)$/)!;
  return { x: Number(x), y: Number(y), scaleX: Number(scaleX), scaleY: Number(scaleY) };
}

it("eases into a neighboring option and snaps across gaps, diagonals and groups", () => {
  const { animate, media } = mockMotion();
  const { rerender } = render(<Options selected="a" />);
  expect(animate).not.toHaveBeenCalled();

  rerender(<Options selected="b" />);
  animate.mock.results[0].value.currentTime = Number(animate.mock.calls[0][1].duration);
  rerender(<Options selected="e" />);
  const [horizontal, vertical] = animate.mock.calls.map(([frames]) => frames.map(transform));
  expect(horizontal[0]).toEqual({ x: -84, y: 0, scaleX: 1, scaleY: 1 });
  expect(vertical[0]).toEqual({ x: 0, y: -44, scaleX: 1, scaleY: 1 });
  expect(horizontal[2].x - horizontal[1].x).toBeGreaterThan(horizontal[1].x - horizontal[0].x);
  expect(Math.max(...horizontal.map(({ x }) => x))).toBeGreaterThan(0);
  expect(Math.max(...horizontal.map(({ x }) => x))).toBeLessThan(2);
  expect(Math.max(...horizontal.map(({ scaleX }) => scaleX))).toBeGreaterThan(1.025);
  expect(Math.max(...vertical.map(({ scaleY }) => scaleY))).toBeGreaterThan(1.025);
  expect(horizontal[horizontal.length - 1]).toEqual({ x: 0, y: 0, scaleX: 1, scaleY: 1 });

  animate.mockClear();
  rerender(<Options selected="outside" />);
  rerender(<Options selected="c" />);
  rerender(<Options selected="a" />);
  rerender(<Options selected="e" />);
  expect(animate).not.toHaveBeenCalled();

  media.matches = true;
  rerender(<Options selected="b" />);
  expect(animate).not.toHaveBeenCalled();
});

it("retains position and velocity when a moving selection reverses", () => {
  const { animate } = mockMotion();
  const { rerender } = render(<Options selected="a" />);
  rerender(<Options selected="b" />);

  const [outbound, timing] = animate.mock.calls[0];
  const moving = transform(outbound[6]);
  animate.mock.results[0].value.currentTime = 6 / (outbound.length - 1) * Number(timing.duration);
  rerender(<Options selected="a" />);

  const [reversed] = animate.mock.calls[1];
  expect(transform(reversed[0]).x).toBeCloseTo(moving.x + 84);
  expect(transform(reversed[0]).scaleX).toBeCloseTo(moving.scaleX);
  expect(transform(reversed[1]).x).toBeGreaterThan(transform(reversed[0]).x);
  expect(transform(reversed[reversed.length - 1]).x).toBe(0);
});
