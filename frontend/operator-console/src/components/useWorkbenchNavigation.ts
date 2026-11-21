import { useLayoutEffect, useRef } from "react";

type WorkbenchPane = "list" | "detail" | "evidence";

export function useWorkbenchNavigation(pane: WorkbenchPane, compactQuery = "(max-width: 1180px)") {
  const heading = useRef<HTMLHeadingElement>(null);
  const previousPane = useRef<WorkbenchPane>("list");
  const positions = useRef<Partial<Record<WorkbenchPane, { trigger: HTMLElement; scrollY: number }>>>({});

  function rememberPosition(trigger: HTMLElement, fromPane: WorkbenchPane = pane): void {
    positions.current[fromPane] = { trigger, scrollY: window.scrollY };
  }

  useLayoutEffect(() => {
    const previous = previousPane.current;
    previousPane.current = pane;
    const compact = window.matchMedia?.(compactQuery).matches;
    if (previous === pane || (!compact && previous !== "evidence" && pane !== "evidence")) return;

    const returning = pane === "list" || (previous === "evidence" && pane === "detail");
    const destination = !compact && previous === "evidence" ? "detail" : pane;
    const position = returning ? positions.current[destination] : undefined;
    if (position?.trigger.isConnected && position.trigger.getClientRects().length) {
      position.trigger.focus({ preventScroll: true });
      window.scrollTo(0, position.scrollY);
    } else {
      heading.current?.focus({ preventScroll: true });
      heading.current?.scrollIntoView?.({ block: "start" });
    }
  }, [compactQuery, pane]);

  return { heading, rememberPosition };
}
