import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { m } from 'motion/react';
import { Icon } from './Icon';
import { homeMotion, visualTest } from './motion';
import { displayTime, viewLabels, type Entry, type Snapshot, type View } from './homeData';

export function Modal({ title, drawer = false, reduced, onClose, children, returnFocus }: { title: string; drawer?: boolean; reduced: boolean; onClose: () => void; children: ReactNode; returnFocus: RefObject<HTMLElement | null> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current!, focus = returnFocus.current;
    const overflow = document.body.style.overflow;
    node.showModal(); document.body.style.overflow = 'hidden';
    const input = node.querySelector<HTMLInputElement>('input');
    if (input) input.focus();
    return () => { node.close(); document.body.style.overflow = overflow; if (focus?.isConnected) focus.focus({ preventScroll: true }); };
  }, [returnFocus]);
  useEffect(() => {
    if (!dialog.current?.querySelector('input')) dialog.current?.querySelector<HTMLHeadingElement>('h2')?.focus();
  }, [title]);
  return createPortal(<dialog ref={dialog} aria-labelledby="dialog-title" className={`modal ${drawer ? 'drawer' : ''}`} onKeyDown={e => {
    if (e.key !== 'Tab') return;
    const controls = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href],[tabindex="0"]')].filter(node => node.getClientRects().length > 0);
    const first = controls[0], last = controls.at(-1);
    if (!first) { e.preventDefault(); return; }
    if (e.shiftKey && (document.activeElement === first || !controls.includes(document.activeElement as HTMLElement))) { e.preventDefault(); last?.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !controls.includes(document.activeElement as HTMLElement))) { e.preventDefault(); first.focus(); }
  }} onCancel={e => { e.preventDefault(); onClose(); }} onClick={e => { if (e.target === e.currentTarget) { const r = e.currentTarget.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) onClose(); } }}>
    <m.div className="modal-inner" initial={reduced || visualTest ? false : { opacity: 0, y: drawer ? 0 : 8, x: drawer ? 14 : 0 }} animate={{ opacity: 1, x: 0, y: 0 }} transition={{ duration: homeMotion.dialog, ease: homeMotion.ease }}>
      <div className="modal-heading"><div><span className="panel-kicker">INTERACTIVE DEMO</span><h2 id="dialog-title" tabIndex={-1}>{title}</h2></div><button className="icon-button" aria-label="Close dialog" onClick={onClose}><Icon name="close" /></button></div>
      {children}
    </m.div>
  </dialog>, document.body);
}

export function DataMessage({ loading, error, retry }: { loading: boolean; error: string | null; retry: () => void }) {
  return <div className="empty-state" role={error ? 'alert' : 'status'}><Icon name={error ? 'source' : 'search'} size={24} /><h3>{error ? 'Demo collection unavailable' : loading ? 'Loading demo collection…' : 'No matching cases'}</h3><p>{error ? 'The local sample could not be read. No replacement data has been inserted.' : loading ? 'Reading the local synthetic snapshot.' : 'Try another search or filter.'}</p>{error && <button className="secondary-button" onClick={retry}>Retry loading</button>}</div>;
}

export function Search({ snapshot, loading, error, retry, openDetail }: { snapshot: Snapshot | null; loading: boolean; error: string | null; retry: () => void; openDetail: (e: Entry) => void }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(true);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setBusy(true); setResults([]); setActive(0);
    // Debounce local work; cancelled queries cannot replace a newer result.
    const timer = window.setTimeout(() => {
      const needle = query.trim().toLocaleLowerCase();
      setResults((snapshot?.entries ?? []).filter(e => `${e.title} ${e.sector} ${e.wallet ?? ''} ${e.fields.map(f => f.value).join(' ')}`.toLocaleLowerCase().includes(needle)));
      setBusy(false);
    }, 120);
    return () => clearTimeout(timer);
  }, [query, snapshot]);
  useEffect(() => { document.getElementById(`search-result-${active}`)?.scrollIntoView({ block: 'nearest' }); }, [active]);
  return <>
    <div className="search-input-wrap"><Icon name="search" /><input ref={input} aria-label="Search demo collection" placeholder="Market, wallet, or research title…" value={query} role="combobox" aria-controls="search-results" aria-expanded="true" aria-autocomplete="list" aria-activedescendant={results[active] ? `search-result-${active}` : undefined} onChange={e => setQuery(e.target.value)} onKeyDown={e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setActive(i => results.length ? (i + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length : 0); }
      if (e.key === 'Enter' && results[active] && !busy) { e.preventDefault(); openDetail(results[active]); }
    }} />{query && <button className="icon-button" aria-label="Clear search" onClick={() => { setQuery(''); input.current?.focus(); }}><Icon name="close" size={16} /></button>}</div>
    <p className="search-scope">Synthetic cases only · no live market or wallet lookup</p>
    <div className="search-results" id="search-results" role="listbox" aria-label="Search results" aria-busy={busy || loading}>
      {!(busy || loading || error) && results.map((entry, i) => <div key={entry.id} id={`search-result-${i}`} role="option" aria-selected={active === i} className={`search-result ${active === i ? 'selected' : ''}`} onMouseEnter={() => setActive(i)} onClick={() => openDetail(entry)}><span className="result-icon"><Icon name={entry.kind === 'markets' ? 'globe' : entry.kind} /></span><span><strong>{entry.title}</strong><small>{viewLabels[entry.kind]} · {entry.sector}</small></span><Icon name="arrow" size={18} /></div>)}
    </div>
    {(busy || loading || error || results.length === 0) && <DataMessage loading={busy || loading} error={error} retry={retry} />}
    <div className="search-footer"><span><kbd>↑</kbd><kbd>↓</kbd> to select <kbd>↵</kbd> to open</span><span><kbd>esc</kbd> to close</span></div>
  </>;
}

export function Detail({ entry }: { entry: Entry }) {
  return <div className="detail-content">
    <div className="detail-tags"><span>{entry.sector}</span><span className="status-tag">{entry.status}</span></div>
    <p className="detail-summary">{entry.summary}</p>
    <p className="synthetic-note">Synthetic example · no real wallet, market, or transaction</p>
    <dl className="detail-fields">{entry.fields.map(field => <div key={field.label}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}</dl>
    {entry.sources && <section className="detail-section"><h3>Source notes <span>{entry.sources.length}</span></h3>{entry.sources.map(source => <div className="source-note" key={source.title}><Icon name="source" /><div><h4>{source.title}</h4><p>{source.context}</p><small>Illustrative source · no external document</small></div></div>)}</section>}
    <section className="detail-section limitations"><h3>Gaps & limitations</h3><ul>{entry.limitations.map(l => <li key={l}>{l}</li>)}</ul></section>
  </div>;
}

export function Explorer({ view, setView, snapshot, loading, error, retry, openDetail, headingRef }: { view: View; setView: (v: View) => void; snapshot: Snapshot | null; loading: boolean; error: string | null; retry: () => void; openDetail: (e: Entry) => void; headingRef: RefObject<HTMLHeadingElement | null> }) {
  const [sector, setSector] = useState('All sectors');
  const [action, setAction] = useState('All actions');
  const [sort, setSort] = useState('default');
  const entries = (snapshot?.entries ?? []).filter(e => e.kind === view && (sector === 'All sectors' || e.sector === sector) && (view !== 'activity' || action === 'All actions' || e.action === action));
  if (sort === 'title') entries.sort((a, b) => a.title.localeCompare(b.title));
  else if (view === 'activity') entries.sort((a, b) => Date.parse(b.observedAt!) - Date.parse(a.observedAt!));
  const views = Object.keys(viewLabels) as View[];
  return <section className="explorer" id="explorer" aria-labelledby="explorer-title">
    <div className="explorer-heading"><div><span className="panel-kicker">THE DEMO COLLECTION</span><h2 id="explorer-title" tabIndex={-1} ref={headingRef}>Explore the evidence.</h2></div><p>{snapshot ? <>Fixed snapshot<br /><time dateTime={snapshot.asOf}>{displayTime(snapshot.asOf)}</time></> : 'Demo snapshot unavailable'}</p></div>
    <div className="explorer-tabs" role="tablist" aria-label="Collection views">{views.map((v, i) => <button key={v} id={`tab-${v}`} role="tab" aria-selected={view === v} aria-controls="collection-panel" tabIndex={view === v ? 0 : -1} onClick={() => setView(v)} onKeyDown={e => {
      const next = e.key === 'ArrowRight' ? (i + 1) % 4 : e.key === 'ArrowLeft' ? (i + 3) % 4 : e.key === 'Home' ? 0 : e.key === 'End' ? 3 : -1;
      if (next >= 0) { e.preventDefault(); setView(views[next]); document.getElementById(`tab-${views[next]}`)?.focus(); }
    }}>{viewLabels[v]}</button>)}</div>
    <div className="explorer-toolbar"><label>Sector<select aria-label="Filter by sector" value={sector} onChange={e => setSector(e.target.value)}>{['All sectors', 'Politics', 'Macro', 'Crypto', 'Technology'].map(s => <option key={s}>{s}</option>)}</select></label>{view === 'activity' && <label>Action<select aria-label="Filter by action" value={action} onChange={e => setAction(e.target.value)}>{['All actions', 'OPEN', 'ADD', 'REDUCE', 'EXIT'].map(s => <option key={s}>{s}</option>)}</select></label>}<label className="sort-filter">Sort<select aria-label="Sort collection" value={sort} onChange={e => setSort(e.target.value)}><option value="default">{view === 'activity' ? 'Observation time' : 'Collection order'}</option><option value="title">Title A–Z</option></select></label></div>
    <div id="collection-panel" role="tabpanel" aria-labelledby={`tab-${view}`} tabIndex={0}>
      {(loading || error || !entries.length) ? <DataMessage loading={loading} error={error} retry={retry} /> : entries.map(entry => <button className="collection-row" key={entry.id} onClick={() => openDetail(entry)}><span className="row-icon"><Icon name={entry.kind === 'markets' ? 'globe' : entry.kind} /></span><span className="row-main"><strong>{entry.title}</strong><span>{entry.sector} · {entry.kind === 'activity' ? `${entry.action} / ${entry.side} · ${entry.amount}` : entry.status}</span></span><span className="row-meta">{entry.observedAt ? displayTime(entry.observedAt) : entry.kind === 'research' ? `${entry.sources?.length ?? 0} illustrative sources` : 'View scope'}<small>Synthetic example</small></span><Icon name="arrow" /></button>)}
    </div>
    <p className="collection-footnote">{entries.length} matching {entries.length === 1 ? 'case' : 'cases'} · {view === 'activity' ? 'All snapshot observations, including older records. The homepage count uses a fixed 24-hour window.' : 'Provided fixture labels are displayed as-is. No qualifications are calculated here.'}</p>
  </section>;
}
