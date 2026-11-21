import { useEffect, useId, useRef } from "react";

const displacementScale = 38;

function bevelMap(width: number, height: number, radius: number): string | null {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) return null;

  const map = context.createImageData(width, height);
  const pixels = map.data;
  const bevel = Math.min(14, radius);
  pixels.fill(128);
  for (let y = 0; y < height; y += 1) {
    const dy = y + 0.5 - height / 2;
    const cornerY = Math.max(0, Math.abs(dy) - (height / 2 - radius));
    for (let x = 0; x < width; x += 1) {
      const offset = 4 * (y * width + x);
      pixels[offset + 3] = 255;
      const dx = x + 0.5 - width / 2;
      const cornerX = Math.max(0, Math.abs(dx) - (width / 2 - radius));
      const distance = Math.hypot(cornerX, cornerY);
      const depth = radius - distance;
      if (depth < 0 || depth >= bevel) continue;

      // Sample inward along the curved edge. The flat center transmits directly.
      const shift = 16 * (1 - depth / bevel) ** 1.4 * 255 / displacementScale;
      pixels[offset] = 128 - Math.sign(dx) * cornerX / distance * shift;
      pixels[offset + 1] = 128 - Math.sign(dy) * cornerY / distance * shift;
    }
  }
  context.putImageData(map, 0, 0);
  return canvas.toDataURL();
}

export function GlassLens() {
  const id = useId();
  const filter = useRef<SVGFilterElement>(null);
  const mapImage = useRef<SVGFEImageElement>(null);

  useEffect(() => {
    const filterElement = filter.current;
    const imageElement = mapImage.current;
    const surface = filterElement?.ownerSVGElement?.parentElement;
    if (!filterElement || !imageElement || !surface) return;

    const reflection = window.matchMedia("(hover: hover) and (prefers-reduced-motion: no-preference)");
    const resetLight = () => {
      surface.style.removeProperty("--glass-light-x");
      surface.style.removeProperty("--glass-light-y");
    };
    const moveLight = (event: PointerEvent) => {
      if (!reflection.matches || event.pointerType === "touch" || document.documentElement.dataset.material === "solid") return;
      if (getComputedStyle(surface, "::after").content === "none") return;
      const bounds = surface.getBoundingClientRect();
      const x = Math.round((event.clientX - bounds.left) / bounds.width * 100);
      const y = Math.round((event.clientY - bounds.top) / bounds.height * 100);
      if (x < 0 || x > 100 || y < 0 || y > 100) {
        resetLight();
        return;
      }
      surface.style.setProperty("--glass-light-x", `${x}%`);
      surface.style.setProperty("--glass-light-y", `${y}%`);
    };
    let edgeOpacity = -1;
    const blendScrollEdge = () => {
      const opacity = Math.max(0, Math.min(1, (window.scrollY - 40) / 65));
      if (opacity === edgeOpacity) return;
      edgeOpacity = opacity;
      surface.style.setProperty("--glass-scroll-opacity", String(opacity));
    };
    surface.addEventListener("pointermove", moveLight, { passive: true });
    surface.addEventListener("pointerleave", resetLight);
    reflection.addEventListener("change", resetLight);
    window.addEventListener("scroll", blendScrollEdge, { passive: true });
    blendScrollEdge();

    // Chromium renders fragment SVG filters against the live backdrop.
    // WebKit uses the same tint and reflections with a native CSS backdrop.
    let observer: ResizeObserver | undefined;
    if ("chrome" in window && window.CSS?.supports("backdrop-filter", "url(#lens)") && typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        const style = getComputedStyle(surface);
        if (style.getPropertyValue("--glass-lens-slot").trim() === "0") {
          delete surface.dataset.glassRefraction;
          return;
        }
        const { width, height } = surface.getBoundingClientRect();
        const w = Math.round(width);
        const h = Math.round(height);
        const radius = Math.min(parseFloat(style.borderTopLeftRadius), h / 2, w / 2);
        if (!w || !h || !radius) return;
        const texture = bevelMap(w, h, radius);
        if (!texture) return;

        for (const element of [filterElement, imageElement]) {
          element.setAttribute("width", String(w));
          element.setAttribute("height", String(h));
        }
        imageElement.setAttribute("href", texture);
        surface.style.setProperty("--glass-refraction", `url("#${id}")`);
        surface.dataset.glassRefraction = "";
      });
      observer.observe(surface, { box: "border-box" });
    }

    return () => {
      observer?.disconnect();
      delete surface.dataset.glassRefraction;
      surface.style.removeProperty("--glass-refraction");
      surface.removeEventListener("pointermove", moveLight);
      surface.removeEventListener("pointerleave", resetLight);
      reflection.removeEventListener("change", resetLight);
      window.removeEventListener("scroll", blendScrollEdge);
      surface.style.removeProperty("--glass-scroll-opacity");
      resetLight();
    };
  }, [id]);

  return (
    <svg aria-hidden="true" className="glass-lens" focusable="false" width="0" height="0">
      <defs>
        <filter ref={filter} id={id} x="0" y="0" filterUnits="userSpaceOnUse" colorInterpolationFilters="sRGB">
          <feImage ref={mapImage} x="0" y="0" preserveAspectRatio="none" result="bevel" />
          <feDisplacementMap in="SourceGraphic" in2="bevel" scale={displacementScale} xChannelSelector="R" yChannelSelector="G" />
        </filter>
      </defs>
    </svg>
  );
}
