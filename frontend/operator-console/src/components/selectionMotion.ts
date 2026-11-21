import { spring } from "motion";

type Axis = { value: number; velocity: number };
export type SelectionPose = { x: Axis; y: Axis; width: Axis; height: Axis };

export function selectionMotion(start: SelectionPose, target: DOMRect, glass: boolean) {
  const duration = glass ? 420 : 330;
  const stiffness = glass ? 520 : 690;
  const damping = glass ? 36.9 : 52;

  function track(from: Axis, to: number) {
    const curve = spring({
      keyframes: [from.value, to],
      velocity: from.velocity,
      stiffness,
      damping,
      restDelta: 0.004,
      restSpeed: 0.08
    });
    return (time: number): Axis => time >= duration
      ? { value: to, velocity: 0 }
      : { value: curve.next(time).value, velocity: curve.velocity?.(time) ?? 0 };
  }

  const x = track(start.x, 0);
  const y = track(start.y, 0);
  const width = track(start.width, target.width);
  const height = track(start.height, target.height);
  const sample = (time: number): SelectionPose => ({ x: x(time), y: y(time), width: width(time), height: height(time) });
  const body: Keyframe[] = [];
  const reflection: Keyframe[] = [];
  const steps = Math.ceil(duration / 8);

  // Position and reflected light use the same physical motion and animation clock.
  for (let index = 0; index <= steps; index++) {
    const pose = sample(index / steps * duration);
    const speedX = pose.x.velocity / pose.width.value;
    const speedY = pose.y.velocity / pose.height.value;
    const energyX = speedX ** 2;
    const energyY = speedY ** 2;
    const stretchX = glass ? 0.058 * energyX / (42 + energyX) : 0;
    const stretchY = glass ? 0.058 * energyY / (42 + energyY) : 0;
    const scaleX = pose.width.value / target.width * (1 + stretchX - stretchY * 0.2);
    const scaleY = pose.height.value / target.height * (1 + stretchY - stretchX * 0.2);
    body.push({
      transform: `translate(${pose.x.value}px, ${pose.y.value}px) scale(${scaleX}, ${scaleY})`
    });
    if (glass) {
      const energy = energyX + energyY;
      const drift = 30 / (5 + Math.sqrt(energy));
      reflection.push({
        transform: `translate(${speedX * drift}%, ${speedY * drift}%)`,
        opacity: 0.84 * energy / (22 + energy)
      });
    }
  }

  return { duration, body, reflection, sample };
}
