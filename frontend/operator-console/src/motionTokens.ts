import type { Transition, Variants } from "motion/react";

/**
 * Motion tokens.
 *
 * Motion here is spatial continuity, not decoration: it shows that a panel
 * replaced another panel, or that a selection moved rather than restarted. The
 * library owns panel and route transitions; hover, press, colour, and focus stay in
 * CSS. Durations stay under 300ms and every movement is paired with opacity so
 * `prefers-reduced-motion` can drop the movement and keep the state change.
 */
export const easeOut: Transition["ease"] = [0.23, 1, 0.32, 1];

export const routeTransition: Transition = { duration: 0.14, ease: easeOut };

export const panelTransition: Transition = { duration: 0.2, ease: easeOut };

export const groupTransition: Transition = { duration: 0.18, ease: easeOut };

/**
 * Route changes cross-fade with no travel. The sidebar is a high-frequency
 * control and macOS does not translate between sidebar destinations either; a
 * slide here would read as the app being slow.
 */
export const routeVariants: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1 }
};

/** Panels rise a few pixels on entry and leave faster than they arrive. */
export const panelVariants: Variants = {
  hidden: { opacity: 0, y: 6 },
  visible: { opacity: 1, y: 0 }
};

export const listItemVariants: Variants = {
  hidden: { opacity: 0, y: 4 },
  visible: { opacity: 1, y: 0 }
};

/**
 * Group content in Settings. The exit is faster than the entrance so a tab
 * change reads as having settled rather than as having played.
 */
export const groupVariants: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { duration: 0.18, ease: easeOut } },
  exit: { opacity: 0, transition: { duration: 0.08, ease: easeOut } }
};
