export type View = 'markets' | 'wallets' | 'activity' | 'research';
export const viewLabels: Record<View, string> = { markets: 'Markets', wallets: 'Sector Wallets', activity: 'Trade Activity', research: 'Evidence & Research' };
export interface Entry {
  id: string; kind: View; title: string; sector: string; summary: string; status: string;
  fields: { label: string; value: string }[];
  limitations: string[];
  sources?: { title: string; context: string }[];
  observedAt?: string;
  action?: 'OPEN' | 'ADD' | 'REDUCE' | 'EXIT';
  side?: 'BUY' | 'SELL';
  amount?: string;
  wallet?: string;
}
export interface Snapshot { mode: 'demo'; asOf: string; entries: Entry[] }

// A local, explicitly synthetic asset. No live endpoint or fallback is invented.
export async function readHomeData(signal: AbortSignal): Promise<Snapshot> {
  const response = await fetch('/demo-snapshot.json', { signal });
  if (!response.ok) throw new Error('The demo collection could not be loaded.');
  const value: unknown = await response.json();
  if (!value || typeof value !== 'object') throw new Error('Invalid demo collection.');
  const data = value as Snapshot;
  const text = (value: unknown) => typeof value === 'string' && value.length > 0;
  if (data.mode !== 'demo' || !text(data.asOf) || !Number.isFinite(Date.parse(data.asOf)) || !Array.isArray(data.entries) ||
    !data.entries.every(e => e && text(e.id) && e.kind in viewLabels && text(e.title) && text(e.sector) && text(e.summary) && text(e.status) &&
      Array.isArray(e.fields) && e.fields.every(f => f && text(f.label) && text(f.value)) &&
      Array.isArray(e.limitations) && e.limitations.every(text) &&
      (e.sources === undefined || (Array.isArray(e.sources) && e.sources.every(s => s && text(s.title) && text(s.context)))) &&
      (e.kind !== 'activity' || (text(e.observedAt) && Number.isFinite(Date.parse(e.observedAt!)) && ['OPEN', 'ADD', 'REDUCE', 'EXIT'].includes(e.action ?? '') && ['BUY', 'SELL'].includes(e.side ?? '') && text(e.amount) && text(e.wallet))))) {
    throw new Error('The demo collection has an unsupported format.');
  }
  if (new Set(data.entries.map(e => e.id)).size !== data.entries.length) throw new Error('Duplicate demo identities.');
  return data;
}

export function getMetrics(snapshot: Snapshot | null) {
  const count = (kind: View) => snapshot ? snapshot.entries.filter(e => e.kind === kind).length : null;
  const end = snapshot ? Date.parse(snapshot.asOf) : 0;
  return [
    { label: 'Observed wallets', value: count('wallets') },
    { label: 'Markets in scope', value: count('markets') },
    { label: 'Observations · 24h', value: snapshot ? snapshot.entries.filter(e => e.kind === 'activity' && Date.parse(e.observedAt!) > end - 86400000 && Date.parse(e.observedAt!) <= end).length : null },
    { label: 'Research cases', value: count('research') },
  ];
}

export const displayTime = (value: string) => new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(new Date(value)) + ' UTC';
