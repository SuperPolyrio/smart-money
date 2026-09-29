import { useSyncExternalStore } from 'react';

const reducedMedia = matchMedia('(prefers-reduced-motion: reduce)');
const subscribe = (changed: () => void) => {
  reducedMedia.addEventListener('change', changed);
  return () => reducedMedia.removeEventListener('change', changed);
};
export const useMotionPreference = () => useSyncExternalStore(subscribe, () => reducedMedia.matches);

export const homeMotion = {
  ease: [0.22, 1, 0.36, 1] as const,
  entrance: .65, stagger: .07, dialog: .24,
  rotation: .3 * Math.PI / 180,
  tilt: 2.5 * Math.PI / 180,
  travel: 3.5,
};

// Only freezes presentation. It never selects data or bypasses a business rule.
export const visualTest = import.meta.env.DEV && new URLSearchParams(location.search).has('visual-test');
