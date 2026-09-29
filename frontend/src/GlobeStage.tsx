import { useEffect, useRef, useState, type ReactNode } from 'react';
import { homeCopy } from './content';
import { Icon } from './Icon';
import type { GlobeScene } from './createGlobeScene';

export function GlobeStage({ reduced, paused, modalOpen, toggle, children }: { reduced: boolean; paused: boolean; modalOpen: boolean; toggle: () => void; children?: ReactNode }) {
  const host = useRef<HTMLDivElement>(null);
  const scene = useRef<GlobeScene | null>(null);
  const [small, setSmall] = useState(() => matchMedia('(max-width: 767px)').matches);
  const [enabled, setEnabled] = useState(false);
  const [visible, setVisible] = useState(true);
  const [onScreen, setOnScreen] = useState(true);
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const constrained = (navigator as Navigator & { connection?: { saveData?: boolean }; deviceMemory?: number }).connection?.saveData || (navigator as Navigator & { deviceMemory?: number }).deviceMemory === 2;
  const poster = reduced || ((small || constrained) && !enabled) || failed;
  useEffect(() => {
    const media = matchMedia('(max-width: 767px)');
    const changed = () => setSmall(media.matches);
    const visibility = () => setVisible(!document.hidden);
    const observer = new IntersectionObserver(entries => setOnScreen(entries[0].isIntersecting), { threshold: .05 });
    observer.observe(host.current!);
    media.addEventListener('change', changed); document.addEventListener('visibilitychange', visibility); visibility();
    return () => { media.removeEventListener('change', changed); document.removeEventListener('visibilitychange', visibility); observer.disconnect(); };
  }, []);
  useEffect(() => {
    if (poster) return;
    let cancelled = false;
    const abort = new AbortController();
    let owned: GlobeScene | null = null;
    setReady(false);
    async function load() {
      let bitmap: ImageBitmap | undefined;
      try {
        const [module, response] = await Promise.all([import('./createGlobeScene'), fetch('/assets/earth-land-mask.png', { signal: abort.signal })]);
        if (!response.ok) throw new Error('Land mask unavailable');
        bitmap = await createImageBitmap(await response.blob());
        if (cancelled) return;
        const canvas = document.createElement('canvas'); canvas.width = bitmap.width; canvas.height = bitmap.height;
        const context = canvas.getContext('2d'); if (!context) throw new Error('Mask decoding unavailable');
        context.drawImage(bitmap, 0, 0);
        owned = module.createGlobeScene(host.current!, context.getImageData(0, 0, canvas.width, canvas.height), small, () => setFailed(true));
        scene.current = owned; setReady(true);
      } catch (error) {
        if (!cancelled) {
          if (import.meta.env.DEV) console.warn('Smart Money globe: static fallback', error);
          setFailed(true);
        }
      }
      finally { bitmap?.close(); }
    }
    void load();
    return () => { cancelled = true; abort.abort(); owned?.dispose(); scene.current = null; };
  }, [poster, small]);
  useEffect(() => { scene.current?.setRunning(ready && !paused && !modalOpen && visible && onScreen && !poster); }, [ready, paused, modalOpen, visible, onScreen, poster]);
  return <div className="globe-stage" data-state={poster ? 'poster' : ready ? 'ready' : 'loading'}>
    <div ref={host} className="globe-canvas" aria-hidden="true" />
    {poster && <img className="globe-poster" src="/assets/globe-poster.webp" alt="" width="800" height="620" />}
    {children}
    <div className="globe-controls">
      {poster ? <span className="static-label">{reduced ? 'Reduced motion' : failed ? 'Static globe' : 'Static preview'}</span> : <button className="motion-toggle" onClick={toggle} aria-label={paused ? 'Resume animation' : 'Pause animation'} aria-pressed={paused}><Icon name={paused ? 'play' : 'pause'} size={15} /></button>}
      {poster && !reduced && !failed && <button className="enable-animation" onClick={() => setEnabled(true)}><Icon name="play" size={13} />Enable animation</button>}
      <p className="globe-caption">{homeCopy.globeCaption}</p>
    </div>
  </div>;
}
