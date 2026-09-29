import { useEffect, useRef, useState } from 'react';
import { LazyMotion, MotionConfig, domAnimation, m } from 'motion/react';
import { homeMotion, visualTest, useMotionPreference } from './motion';
import { GlobeStage } from './GlobeStage';
import { homeCopy as copy } from './content';
import { Icon } from './Icon';
import { Detail, Explorer, Modal, Search } from './Panels';
import { displayTime, getMetrics, readHomeData, type Entry, type Snapshot, type View } from './homeData';

export default function App() {
  const [paused, setPaused] = useState(false);
  const reduced = useMotionPreference();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [view, setView] = useState<View | null>(null);
  const [navigation, setNavigation] = useState(0);
  const [menu, setMenu] = useState(false);
  const [modal, setModal] = useState<'search' | 'api' | 'scope' | Entry | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const explorerHeading = useRef<HTMLHeadingElement>(null);
  const searchButton = useRef<HTMLButtonElement>(null);
  const retry = () => setAttempt(i => i + 1);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError(null); setSnapshot(null);
    readHomeData(abort.signal).then(data => { if (!abort.signal.aborted) setSnapshot(data); })
      .catch(() => { if (!abort.signal.aborted) setError('Demo collection unavailable'); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [attempt]);
  function openModal(value: NonNullable<typeof modal>) {
    if (!modal) returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : searchButton.current;
    setModal(value);
  }
  function openView(next: View) { setMenu(false); setView(next); setNavigation(i => i + 1); }
  useEffect(() => {
    if (!view) return;
    explorerHeading.current?.focus({ preventScroll: true });
    explorerHeading.current?.scrollIntoView({ behavior: reduced || visualTest ? 'instant' : 'smooth', block: 'start' });
  }, [view, navigation, reduced]);
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if (event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey) && !event.altKey && !(event.target instanceof Element && event.target.closest('input,textarea,select,[contenteditable="true"]'))) {
        event.preventDefault();
        if (!modal) { returnFocus.current = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : searchButton.current; setModal('search'); }
      }
      if (event.key === 'Escape' && menu) { setMenu(false); document.getElementById('menu-toggle')?.focus(); }
    }
    document.addEventListener('keydown', shortcut);
    return () => document.removeEventListener('keydown', shortcut);
  }, [modal, menu]);
  const observations = snapshot?.entries.filter(e => e.kind === 'activity').slice(0, 3) ?? [];
  const motionProps = (delay: number) => ({ initial: reduced || visualTest ? false as const : { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: homeMotion.entrance, delay, ease: homeMotion.ease } });
  return <LazyMotion features={domAnimation}><MotionConfig reducedMotion={reduced ? 'always' : 'never'}>
    <a className="skip-link" href="#main">Skip to content</a>
    <header className="header container">
      <a href="#main" className="brand"><img src="/assets/mark.svg" alt="" />{copy.brand}</a>
      <nav id="main-navigation" className={menu ? 'menu-open' : ''} aria-label="Main navigation">{(['Markets', 'Traders', 'Insights', 'API'] as const).map(n => <button key={n} onClick={() => n === 'API' ? openModal('api') : openView(n === 'Markets' ? 'markets' : n === 'Traders' ? 'wallets' : 'research')}>{n}</button>)}</nav>
      <button ref={searchButton} className="search-trigger" aria-label="Search Smart Money" aria-haspopup="dialog" onClick={() => openModal('search')}><Icon name="search" /><span>Search markets, traders, or themes…</span><kbd>⌘ K</kbd></button>
      <button className="button header-cta" onClick={() => openView('activity')}>Explore demo</button><button className="menu-toggle icon-button" id="menu-toggle" aria-label={menu ? 'Close navigation' : 'Open navigation'} aria-expanded={menu} aria-controls="main-navigation" onClick={() => setMenu(!menu)}><Icon name={menu ? 'close' : 'menu'} /></button>
    </header>
    <main id="main" className="container" tabIndex={-1}>
      <section className="hero" aria-labelledby="hero-title">
        <m.div className="hero-copy" initial={false} animate="visible" variants={{visible:{transition:{staggerChildren:homeMotion.stagger}}}}>
          <m.p className="eyebrow" initial={reduced || visualTest ? false : {opacity:0,y:12}} animate={{opacity:1,y:0}} transition={{duration:homeMotion.entrance,ease:homeMotion.ease}}>{copy.eyebrow}</m.p>
          <m.h1 id="hero-title" initial={reduced || visualTest ? false : {opacity:0,y:12}} animate={{opacity:1,y:0}} transition={{duration:homeMotion.entrance,delay:.07,ease:homeMotion.ease}}>{copy.title[0]}<br />{copy.title[1]}</m.h1>
          <m.p className="subtitle" {...motionProps(.14)}>{copy.subtitle}</m.p>
          <m.button className="button primary-cta" {...motionProps(.21)} onClick={() => openView('activity')}>{copy.cta}<Icon name="arrow" size={23} /></m.button>
          <span className="demo-label"><i />{copy.demo}<span className="demo-note">Synthetic cases. Real interactions.</span></span>
        </m.div>
        <GlobeStage reduced={reduced} paused={paused} modalOpen={modal !== null} toggle={() => setPaused(!paused)}>
          {observations.map((entry, i) => <button key={entry.id} className={`observation-overlay overlay-${i}`} onClick={() => openModal(entry)} aria-label={`Open ${entry.wallet} demo observation`}><span className="overlay-icon"><Icon name={i === 0 ? 'source' : i === 1 ? 'globe' : 'activity'} size={23} /></span><span><small>{entry.wallet} · demo</small><strong className={entry.side === 'SELL' ? 'sell' : ''}>{entry.side} · {entry.amount}</strong><span>{entry.sector} · {entry.action}</span></span></button>)}
        </GlobeStage>
        <button className="metrics" onClick={() => openModal('scope')} aria-label="View demo statistics and scope">{getMetrics(snapshot).map(metric => <span className="metric" key={metric.label}><strong>{metric.value ?? '—'}</strong><span>{metric.label}</span></span>)}</button>
        <button className="activity-strip" onClick={() => openView('activity')}><i />Demo activity<span>{snapshot ? `${getMetrics(snapshot)[2].value} observations · fixed snapshot` : 'View collection status'}</span><Icon name="arrow" /></button>
        {error && <div className="home-data-error" role="alert">Demo unavailable <button onClick={retry}>Retry loading</button></div>}
      </section>
      <section className="feature-grid" aria-label="Explore Smart Money features">{copy.features.map(feature => <button className={`feature-card ${feature.id}`} key={feature.id} onClick={() => openView(feature.id)}>
        <span className="feature-icon"><Icon name={feature.icon} /></span><h2>{feature.title}</h2><p>{feature.description}</p><Icon name="arrow" />
        {feature.id === 'research' ? <div className="research-art" aria-hidden="true">{['Rule source', 'Trade context', 'Update recorded'].map((label, i) => <div key={label}><Icon name="source" size={16} /><span>{label}<small>{i + 1} / Evidence note</small></span></div>)}</div> : <svg className="feature-art" viewBox="0 0 280 190" fill="none" aria-hidden="true"><defs><linearGradient id={`fade-${feature.id}`} x1="0" y1="0" x2="1" y2="1"><stop stopColor="#aebfc9" stopOpacity=".2" /><stop offset="1" stopColor="#aebfc9" stopOpacity="0" /></linearGradient></defs><path d="M0 171C35 175 37 138 69 140S119 177 148 115 181 120 209 92 254 102 280 38V190H0Z" fill={`url(#fade-${feature.id})`} /><path d="M0 171C35 175 37 138 69 140S119 177 148 115 181 120 209 92 254 102 280 38" stroke="#a9bac6" strokeOpacity=".6" /><path d="M0 148C55 170 46 80 80 90S106 145 142 65 168 89 201 44 238 80 280 16" stroke="#71848e" strokeOpacity=".16" />{feature.id === 'activity' && <><circle cx="209" cy="92" r="10" fill="#c8d7e3" opacity=".18" /><circle cx="209" cy="92" r="4" fill="#e2e9ed" /></>}</svg>}
      </button>)}</section>
      {view && <Explorer view={view} setView={setView} snapshot={snapshot} loading={loading} error={error} retry={retry} openDetail={openModal} headingRef={explorerHeading} />}
    </main>
    <footer className="footer container"><span>{copy.footer}</span><span>{copy.principle}</span></footer>
    {modal && <Modal title={typeof modal === 'object' ? modal.title : modal === 'search' ? 'Find your context.' : modal === 'api' ? 'Integration preview' : 'A small, transparent collection.'} drawer={typeof modal === 'object'} reduced={reduced} returnFocus={returnFocus} onClose={() => setModal(null)}>
      {modal === 'search' ? <Search snapshot={snapshot} loading={loading} error={error} retry={retry} openDetail={openModal} /> : typeof modal === 'object' ? <Detail entry={modal} /> : modal === 'api' ? <div className="info-content"><p>The read-only API is not connected.</p><p>This homepage reads a local, synthetic collection. There is no public API documentation or live endpoint available in this workspace.</p><div className="integration-status"><span>Data source</span><strong>Local demo snapshot</strong><span>Read-only integration</span><strong>Not connected</strong><span>Wallet connection & orders</span><strong>Not available</strong></div><p>Search and explore the demo directly; no account or wallet is required.</p><button className="secondary-button" onClick={() => setModal('search')}>Search the demo<Icon name="arrow" /></button></div> : <div className="info-content"><p>All counts refer only to this synthetic collection, not to Polymarket-wide coverage.</p><dl className="detail-fields">{getMetrics(snapshot).map(metric => <div key={metric.label}><dt>{metric.label}</dt><dd>{metric.value ?? '—'}</dd></div>)}</dl><p>{snapshot ? `Fixed as-of: ${displayTime(snapshot.asOf)}. The 24-hour window excludes older observations and includes records up to this cutoff.` : 'The collection is unavailable. Unknown counts are shown as —, not zero.'}</p><p>Wallets and markets are unique fixture identities. Research cases are readable synthetic previews, not published reports. No amount, qualification, location or return is asserted as a real business fact.</p></div>}
    </Modal>}
  </MotionConfig></LazyMotion>;
}
