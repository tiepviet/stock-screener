import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { api, getToken, setToken } from './api'
import './styles.css'

const DEFAULT_SETTINGS = {
  capital: 10_000_000,
  risk_percent: 1,
  hard_stop_percent: 7,
  lookback_days: 365,
  language: 'EN',
}

const TICKER_DEFAULTS = ['7203', '6758', '9984', '8306', '6861', '4063', '6501', '9432']
const NAV = [
  ['chart', 'Chart', 'M3 3v18h18M7 15l3-4 3 2 4-6'],
  ['screener', 'Screener', 'M4 5h16M7 12h10M10 19h4'],
  ['signals', 'Signals', 'M4 18l4-5 3 2 5-7 4 3M4 21h16'],
  ['backtest', 'Backtest', 'M4 19V5m0 14h16M8 15l3-3 3 2 4-6'],
  ['portfolio', 'Portfolio', 'M4 19V5m0 14h16M7 16l3-3 3 2 5-6'],
  ['earnings', 'Earnings', 'M6 3v4m6-4v4m6-4v4M4 9h16M5 5h14v15H5z'],
  ['smart', 'Smart Screen', 'M12 3l2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z'],
  ['mtf', 'MTF', 'M4 17l5-5 3 3 8-9M16 6h4v4'],
  ['profit', 'Profit Target', 'M12 3v18M17 7.5c0-1.4-2-2.5-5-2.5S7 6.1 7 7.5 9 10 12 10s5 1.1 5 2.5S15 15 12 15s-5-1.1-5-2.5'],
  ['price-target', 'Price Targets', 'M3 12h4l3-8 4 16 3-8h4'],
  ['alerts', 'Alerts', 'M18 8a6 6 0 00-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4'],
  ['settings', 'Settings', 'M12 15.5A3.5 3.5 0 1112 8a3.5 3.5 0 010 7.5zM19.4 15a1.7 1.7 0 00.3 1.9l.1.1-1.8 1.8-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5v.1h-2.5v-.1a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.9.3l-.1.1-1.8-1.8.1-.1A1.7 1.7 0 009 15a1.7 1.7 0 00-1.5-1H7.4v-2.5h.1a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.9l-.1-.1 1.8-1.8.1.1a1.7 1.7 0 001.9.3 1.7 1.7 0 001-1.5v-.1H15v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1 1.8 1.8-.1.1a1.7 1.7 0 00-.3 1.9 1.7 1.7 0 001.5 1h.1V14h-.1a1.7 1.7 0 00-1.5 1z'],
  ['guide', 'Guide', 'M4 5.5A2.5 2.5 0 016.5 3H20v16H6.5A2.5 2.5 0 004 21.5zm0 0v16M8 7h8m-8 4h8'],
]

function cn(...values) { return values.filter(Boolean).join(' ') }
function fmt(value, digits = 0) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number' && !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value)
}
function yen(value, digits = 0) { return value === null || value === undefined ? '—' : `¥${fmt(value, digits)}` }
function pct(value, digits = 1) { return value === null || value === undefined ? '—' : `${(Number(value) * 100).toFixed(digits)}%` }
function date(value) { if (!value) return '—'; try { return new Date(value).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' }) } catch { return String(value) } }
function useDebouncedValue(value, delay = 350) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => { const timer = setTimeout(() => setDebounced(value), delay); return () => clearTimeout(timer) }, [value, delay])
  return debounced
}
function useAsync(fn, deps = [], initial = null, autoRun = true) {
  const [value, setValue] = useState(initial)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const requestId = useRef(0)
  const run = async (...args) => {
    const id = ++requestId.current
    setValue(initial)
    setLoading(true); setError('')
    try {
      const result = await fn(...args)
      if (id === requestId.current) setValue(result)
      return result
    } catch (e) {
      if (id === requestId.current) setError(e.message || 'Unexpected error')
      return null
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }
  useEffect(() => { if (autoRun) void run() }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return { value, setValue, loading, error, run }
}
function Icon({ path, size = 18 }) { return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={path} /></svg> }
function Button({ children, variant = 'primary', loading, className, ...props }) { return <button className={cn('btn', `btn-${variant}`, className)} disabled={loading || props.disabled} aria-busy={loading || undefined} {...props}>{loading ? <><span className="spinner" /><span className="sr-only">Working…</span></> : children}</button> }
function Field({ label, hint, children, className }) { return <label className={cn('field', className)}><span className="field-label">{label}</span>{children}{hint && <span className="field-hint">{hint}</span>}</label> }
function Select({ children, ...props }) { return <select className="input" {...props}>{children}</select> }
function Input({ type = 'text', ...props }) { return <input className="input" type={type} {...props} /> }
function Textarea(props) { return <textarea className="input textarea" {...props} /> }
function Toggle({ checked, onChange, label }) { return <label className="toggle"><input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} /><span className="toggle-track" /><span>{label}</span></label> }
function Stat({ label, value, detail, tone = '' }) { return <div className={cn('stat', tone && `stat-${tone}`)}><div className="stat-label">{label}</div><div className="stat-value">{value}</div>{detail && <div className="stat-detail">{detail}</div>}</div> }
function Alert({ children, kind = 'info', onClose }) { if (!children) return null; return <div className={cn('alert', `alert-${kind}`)} role="alert" aria-live="polite"><span>{children}</span>{onClose && <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>}</div> }
function Panel({ title, subtitle, action, children, className }) { return <section className={cn('panel', className)}><div className="panel-head"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>{children}</section> }
function Table({ columns, rows, empty = 'No data' }) { if (!rows?.length) return <div className="empty">{empty}</div>; return <div className="table-wrap"><table><thead><tr>{columns.map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead><tbody>{rows.map((row, i) => <tr key={row.id ?? row.ticker ?? i}>{columns.map((c) => <td key={c.key}>{c.render ? c.render(row) : row[c.key] ?? '—'}</td>)}</tr>)}</tbody></table></div> }
function TickerInput({ value, onChange, rows = 5 }) { return <Textarea value={value} onChange={(e) => onChange(e.target.value)} rows={rows} spellCheck="false" /> }
function normalizeTickers(value) { return [...new Set(value.split(/[\s,]+/).map((t) => t.trim().toUpperCase()).filter(Boolean))].slice(0, 100) }
function normalizeSettings(data) {
  const value = data || {}
  const risk = value.risk_percent ?? value.risk_pct ?? ((value.risk_per_trade ?? value.risk_fraction ?? 0.01) * 100)
  const hardStop = value.hard_stop_percent ?? ((value.hard_stop_pct ?? value.hard_stop ?? 0.07) * (Number(value.hard_stop_percent) > 1 ? 1 : 100))
  return {
    ...DEFAULT_SETTINGS,
    ...value,
    capital: value.capital ?? value.total_capital ?? value.sb_capital ?? DEFAULT_SETTINGS.capital,
    risk_percent: Number(risk),
    hard_stop_percent: Number(hardStop),
    lookback_days: value.lookback_days ?? value.sb_lookback ?? DEFAULT_SETTINGS.lookback_days,
    language: value.language ?? value.sb_lang ?? DEFAULT_SETTINGS.language,
  }
}
function settingsPayload(settings) {
  return {
    capital: Number(settings.capital),
    risk_per_trade: Number(settings.risk_percent) / 100,
    risk_pct: Number(settings.risk_percent),
    hard_stop_pct: Number(settings.hard_stop_percent) / 100,
    lookback_days: Number(settings.lookback_days),
    language: settings.language,
  }
}
function SectionTitle({ children, kicker }) { return <div className="section-title">{kicker && <div className="kicker">{kicker}</div>}<h1>{children}</h1></div> }

function App() {
  const [user, setUser] = useState(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [authError, setAuthError] = useState('')
  const [view, setView] = useState('chart')
  const [settings, setSettings] = useState(DEFAULT_SETTINGS)
  const [settingsLoaded, setSettingsLoaded] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    const handleExpired = () => setUser(null)
    window.addEventListener('tse-auth-expired', handleExpired)
    return () => window.removeEventListener('tse-auth-expired', handleExpired)
  }, [])
  useEffect(() => {
    if (!getToken()) { setAuthLoading(false); return }
    api.me().then(setUser).catch(() => setToken(null)).finally(() => setAuthLoading(false))
  }, [])
  useEffect(() => {
    let active = true
    if (!user) { setSettingsLoaded(false); return () => { active = false } }
    setSettingsLoaded(false)
    api.getSettings()
      .then((data) => { if (active) setSettings(normalizeSettings(data)) })
      .catch(() => {})
      .finally(() => { if (active) setSettingsLoaded(true) })
    return () => { active = false }
  }, [user])

  async function login(event) {
    event.preventDefault(); setAuthError(''); setAuthLoading(true)
    const form = new FormData(event.currentTarget)
    try { const result = await api.login(form.get('username'), form.get('password')); setToken(result.access_token); setUser(result.user) }
    catch (e) { setAuthError(e.message) }
    finally { setAuthLoading(false) }
  }
  async function logout() { try { await api.logout() } catch { /* local logout still succeeds */ } setToken(null); setUser(null) }

  if (authLoading && !user) return <div className="boot"><div className="brand-mark">JP</div><span>Loading workspace…</span></div>
  if (!user) return <Login onLogin={login} loading={authLoading} error={authError} />
  if (!settingsLoaded) return <div className="boot"><div className="brand-mark">JP</div><span>Loading settings…</span></div>
  return <Shell user={user} settings={settings} setSettings={setSettings} view={view} setView={setView} logout={logout} sidebarOpen={sidebarOpen} setSidebarOpen={setSidebarOpen} />
}

function Login({ onLogin, loading, error }) {
  const [language, setLanguage] = useState('EN')
  const vn = language === 'VN'
  return <main className="login-page"><div className="login-glow glow-a" /><div className="login-glow glow-b" /><div className="login-card"><div className="login-brand"><div className="brand-mark">JP</div><div><h1>TSE Screener</h1><p>{vn ? 'Sàn giao dịch chứng khoán Tokyo' : 'Tokyo Stock Exchange'}</p></div></div><div className="login-tabs"><button className={language === 'EN' ? 'active' : ''} onClick={() => setLanguage('EN')}>English</button><button className={language === 'VN' ? 'active' : ''} onClick={() => setLanguage('VN')}>Tiếng Việt</button></div>{error && <Alert kind="error">{error}</Alert>}<form onSubmit={onLogin}><Field label={vn ? 'Tên đăng nhập' : 'Username'}><Input name="username" autoComplete="username" placeholder="admin" required /></Field><Field label={vn ? 'Mật khẩu' : 'Password'}><Input name="password" type="password" autoComplete="current-password" placeholder="••••••••" required /></Field><Button type="submit" loading={loading} className="full-button">{vn ? 'Đăng nhập' : 'Sign in'} →</Button></form><p className="login-foot">JWT secured · Tokyo market data</p></div></main>
}

function Shell({ user, settings, setSettings, view, setView, logout, sidebarOpen, setSidebarOpen }) {
  const [profileOpen, setProfileOpen] = useState(false)
  const saveQueue = useRef(Promise.resolve())
  const saveGeneration = useRef(0)
  const current = NAV.find(([id]) => id === view) || NAV[0]

  useEffect(() => () => { saveGeneration.current += 1 }, [])

  function updateSettings(patch) {
    const next = { ...settings, ...patch }
    const generation = ++saveGeneration.current
    setSettings(next)
    saveQueue.current = saveQueue.current
      .catch(() => {})
      .then(() => {
        if (generation !== saveGeneration.current) return undefined
        return api.saveSettings(settingsPayload(next))
      })
    saveQueue.current.catch(() => {})
  }

  function resetSettings() {
    const generation = ++saveGeneration.current
    saveQueue.current = saveQueue.current
      .catch(() => {})
      .then(() => {
        if (generation !== saveGeneration.current) return undefined
        return api.resetSettings()
      })
    return saveQueue.current
  }
  return <div className="app-shell"><aside className={cn('sidebar', sidebarOpen && 'sidebar-open')} aria-label="Primary navigation"><div className="sidebar-top"><div className="logo"><div className="brand-mark small">JP</div><div><strong>TSE</strong><span>Screener</span></div></div><button className="icon-btn sidebar-close" onClick={() => setSidebarOpen(false)}>×</button></div><div className="market-status"><span className="status-dot" /><span>Market data</span><small>JST</small></div><nav>{NAV.map(([id, label, path]) => <button key={id} aria-current={view === id ? 'page' : undefined} className={cn('nav-item', view === id && 'active')} onClick={() => { setView(id); setSidebarOpen(false) }}><Icon path={path} /><span>{label}</span>{id === 'alerts' && <span className="nav-badge">3</span>}</button>)}</nav><div className="sidebar-bottom"><div className="user-chip"><div className="avatar">{user.username.slice(0, 2).toUpperCase()}</div><div><strong>{user.username}</strong><small>{user.can_send_alerts ? 'Administrator' : 'Analyst'}</small></div><button className="icon-btn" aria-label="Open account menu" aria-expanded={profileOpen} onClick={() => setProfileOpen(!profileOpen)}>•••</button></div>{profileOpen && <div className="profile-menu"><button onClick={logout}>Sign out</button></div>}<div className="sidebar-footer">v2.0 · API powered</div></div></aside><div className="main-area"><header className="topbar"><button className="icon-btn menu-toggle" aria-label="Open navigation" onClick={() => setSidebarOpen(true)}>☰</button><div className="breadcrumb"><span>Workspace</span><b>/</b><strong>{current[1]}</strong></div><div className="topbar-actions"><div className="live-pill"><span className="status-dot" /> LIVE</div><button className="icon-btn notification-btn" aria-label="Notifications">♢<i /></button><div className="top-avatar">{user.username.slice(0, 2).toUpperCase()}</div></div></header><main className="content"><div className="content-inner"><Dashboard view={view} settings={settings} updateSettings={updateSettings} setSettings={setSettings} resetSettings={resetSettings} user={user} /></div></main></div>{sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />}</div>
}

function Dashboard({ view, settings, updateSettings, setSettings, resetSettings, user }) {
  switch (view) {
    case 'screener': return <ScreenerView settings={settings} />
    case 'signals': return <SignalsView settings={settings} />
    case 'backtest': return <BacktestView settings={settings} />
    case 'portfolio': return <PortfolioView settings={settings} />
    case 'earnings': return <EarningsView />
    case 'smart': return <SmartView settings={settings} />
    case 'mtf': return <MtfView />
    case 'profit': return <ProfitView />
    case 'price-target': return <PriceTargetView settings={settings} />
    case 'alerts': return <AlertsView user={user} />
    case 'settings': return <SettingsView settings={settings} updateSettings={updateSettings} setSettings={setSettings} resetSettings={resetSettings} />
    case 'guide': return <GuideView />
    default: return <ChartView settings={settings} />
  }
}

function ChartView({ settings }) {
  const [tickerInput, setTickerInput] = useState('7203')
  const ticker = useDebouncedValue(tickerInput)
  const [interval, setIntervalValue] = useState('1d')
  const [lookback, setLookback] = useState(settings.lookback_days)
  const [showSma, setShowSma] = useState(true)
  const [showVolume, setShowVolume] = useState(true)
  const chart = useAsync(() => api.chart(ticker, { interval, lookback_days: lookback, indicators: 'sma20,sma50,sma200,volume' }), [ticker, interval, lookback])
  const candles = chart.value?.candles || []
  const latest = candles[candles.length - 1]
  return <><SectionTitle kicker="MARKET OVERVIEW">Price action</SectionTitle><div className="toolbar"><Field label="Ticker"><Input value={tickerInput} onChange={(e) => setTickerInput(e.target.value.toUpperCase())} /></Field><Field label="Interval"><Select value={interval} onChange={(e) => setIntervalValue(e.target.value)}><option>1d</option><option>1h</option><option>1wk</option></Select></Field><Field label="Lookback (days)"><Input type="number" min="30" max="1095" value={lookback} onChange={(e) => setLookback(e.target.value)} /></Field><Button onClick={() => chart.run()} loading={chart.loading}>Refresh data</Button></div><div className="stat-grid">{[['Last close', latest ? yen(latest.close) : '—', latest?.date], ['Change', latest?.change_pct != null ? pct(latest.change_pct) : '—', 'vs prior close'], ['SMA 20', latest?.sma_20 ? yen(latest.sma_20) : '—', 'technical trend'], ['Volume', latest?.volume ? fmt(latest.volume) : '—', 'latest bar']].map(([label, value, detail]) => <Stat key={label} label={label} value={value} detail={detail} />)}</div><Panel title={`${ticker || '—'} · ${interval}`} subtitle="Candlestick and technical overlays" action={<div className="inline-controls"><Toggle checked={showSma} onChange={setShowSma} label="SMA" /><Toggle checked={showVolume} onChange={setShowVolume} label="Volume" /></div>}>{chart.error && <Alert kind="error">{chart.error}</Alert>}<CandlestickChart candles={candles} showSma={showSma} showVolume={showVolume} /></Panel>{candles.length > 0 && <Panel title="Latest observations" subtitle={`${candles.length} bars returned by the data provider`}><Table columns={[{ key: 'date', label: 'Date' }, { key: 'open', label: 'Open', render: (r) => yen(r.open) }, { key: 'high', label: 'High', render: (r) => yen(r.high) }, { key: 'low', label: 'Low', render: (r) => yen(r.low) }, { key: 'close', label: 'Close', render: (r) => yen(r.close) }, { key: 'volume', label: 'Volume', render: (r) => fmt(r.volume) }]} rows={candles.slice(-10).reverse()} /></Panel>}</>
}

function CandlestickChart({ candles, showSma, showVolume }) {
  if (!candles.length) return <div className="chart-empty">Load a ticker to render the chart.</div>
  const width = 900; const height = 360; const pad = { top: 20, right: 20, bottom: 28, left: 55 }; const chartHeight = showVolume ? 250 : 300; const volumeTop = 275; const lows = candles.map((c) => c.low).filter(Number.isFinite); const highs = candles.map((c) => c.high).filter(Number.isFinite); const min = Math.min(...lows); const max = Math.max(...highs); const range = max - min || 1; const x = (i) => pad.left + (i * (width - pad.left - pad.right) / Math.max(candles.length - 1, 1)); const y = (v) => pad.top + (max - v) * (chartHeight - pad.top) / range; const path = (key) => { let active = false; return candles.map((c, i) => { if (c[key] == null) { active = false; return null } const point = `${active ? 'L' : 'M'}${x(i).toFixed(1)},${y(c[key]).toFixed(1)}`; active = true; return point }).filter(Boolean).join(' ') }; const volumeMax = Math.max(...candles.map((c) => c.volume || 0), 1); return <div className="chart-wrap"><svg viewBox={`0 0 ${width} 390`} className="chart" role="img" aria-label="Candlestick chart">{[0, .25, .5, .75, 1].map((r) => { const value = max - range * r; return <g key={r}><line x1={pad.left} x2={width - pad.right} y1={y(value)} y2={y(value)} className="grid-line" /><text x={4} y={y(value) + 4} className="axis-text">{fmt(value)}</text></g> })}{candles.map((c, i) => { const positive = c.close >= c.open; const color = positive ? '#22c55e' : '#ef5350'; return <g key={c.date ?? i}><line x1={x(i)} x2={x(i)} y1={y(c.high)} y2={y(c.low)} stroke={color} /><rect x={x(i) - Math.max(1.5, 5 * candles.length / width)} y={Math.min(y(c.open), y(c.close))} width={Math.max(3, 10 * candles.length / width)} height={Math.max(1, Math.abs(y(c.open) - y(c.close)))} fill={color} />{showVolume && <rect x={x(i) - 2} y={volumeTop + 55 - (c.volume || 0) / volumeMax * 45} width={4} height={(c.volume || 0) / volumeMax * 45} className="volume-bar" />}</g> })}{showSma && candles.some((c) => c.sma_20 != null) && <><path d={path('sma_20')} className="sma-line sma-20" /><path d={path('sma_50')} className="sma-line sma-50" /><path d={path('sma_200')} className="sma-line sma-200" /></>}{showVolume && <><line x1={pad.left} x2={width - pad.right} y1={volumeTop + 55} y2={volumeTop + 55} className="grid-line" /><text x={4} y={volumeTop + 67} className="axis-text">VOL</text></>}</svg><div className="chart-legend"><span><i className="legend-line green" /> SMA 20</span><span><i className="legend-line orange" /> SMA 50</span><span><i className="legend-line purple" /> SMA 200</span></div></div>
}

function ScreenerView({ settings }) {
  const [tickers, setTickers] = useState(TICKER_DEFAULTS.join('\n')); const [minRoe, setMinRoe] = useState(8); const [maxPe, setMaxPe] = useState(20); const [maxPb, setMaxPb] = useState(2); const [minDiv, setMinDiv] = useState(1); const [watchOnly, setWatchOnly] = useState(false)
  const result = useAsync(() => api.fundamentalScreen({ tickers: normalizeTickers(tickers), min_roe: minRoe / 100, max_pe: maxPe, max_pb: maxPb, min_dividend_yield: minDiv / 100, watchlist_only: watchOnly }), [], null, false)
  const [watchlist, setWatchlist] = useState([]); useEffect(() => { api.getWatchlist().then(setWatchlist).catch(() => {}) }, [])
  const rows = result.value?.results || []
  async function addAll() { const passed = rows.map((r) => r.ticker); if (passed.length) { await api.addWatchlist(passed); setWatchlist(await api.getWatchlist()) } }
  return <><SectionTitle kicker="FUNDAMENTALS">Value screener</SectionTitle><div className="grid-two"><Panel title="Filters" subtitle="All conditions must pass"><div className="form-grid"><Field label="Min ROE (%)"><Input type="number" value={minRoe} onChange={(e) => setMinRoe(+e.target.value)} /></Field><Field label="Max P/E"><Input type="number" value={maxPe} onChange={(e) => setMaxPe(+e.target.value)} /></Field><Field label="Max P/B"><Input type="number" step="0.1" value={maxPb} onChange={(e) => setMaxPb(+e.target.value)} /></Field><Field label="Min dividend (%)"><Input type="number" step="0.1" value={minDiv} onChange={(e) => setMinDiv(+e.target.value)} /></Field></div><Field label="Ticker universe" hint="One ticker per line"><TickerInput value={tickers} onChange={setTickers} rows={9} /></Field><Toggle checked={watchOnly} onChange={setWatchOnly} label="Only my watchlist" /><Button onClick={() => result.run()} loading={result.loading}>Run screener</Button></Panel><Panel title="Watchlist" subtitle={`${watchlist.length} saved symbols`} action={rows.length > 0 && <Button variant="secondary" onClick={addAll}>Add results</Button>}>{result.error && <Alert kind="error">{result.error}</Alert>}<div className="watchlist">{watchlist.length ? watchlist.map((t) => <button className="chip chip-removable" key={t} onClick={async () => { await api.removeWatchlist(t); setWatchlist((old) => old.filter((x) => x !== t)) }}>{t}<span>×</span></button>) : <div className="empty">No saved symbols</div>}</div></Panel></div><Panel title="Screening results" subtitle={rows.length ? `${rows.length} stocks passed` : 'Run a screen to see matches'} action={<span className="muted">EPS must be positive</span>}><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'pe', label: 'P/E', render: (r) => fmt(r.pe, 1) }, { key: 'pb', label: 'P/B', render: (r) => fmt(r.pb, 2) }, { key: 'roe', label: 'ROE', render: (r) => pct(r.roe) }, { key: 'eps', label: 'EPS', render: (r) => fmt(r.eps, 2) }, { key: 'dividend_yield', label: 'Yield', render: (r) => pct(r.dividend_yield, 2) }, { key: 'sector', label: 'Sector' }]} rows={rows} empty={result.loading ? 'Screening market data…' : 'No stocks passed these filters.'} /></Panel></>
}

function SignalsView({ settings }) {
  const [tickers, setTickers] = useState(TICKER_DEFAULTS.slice(0, 5).join('\n')); const [lookback, setLookback] = useState(settings.lookback_days); const [includeSell, setIncludeSell] = useState(true)
  const result = useAsync(() => api.signalScan({ tickers: normalizeTickers(tickers), lookback_days: +lookback, strategies: includeSell ? ['VolumeBreakout', 'PullbackMA', 'TrendBreakdown', 'OverboughtReversal'] : ['VolumeBreakout', 'PullbackMA'], capital_jpy: settings.capital, risk_per_trade: settings.risk_percent / 100, hard_stop_pct: settings.hard_stop_percent / 100 }), [], null, false)
  const signals = result.value?.signals || []; const plans = result.value?.position_plans || []
  return <><SectionTitle kicker="TECHNICAL ENGINE">Signal scanner</SectionTitle><Panel title="Scan configuration" subtitle="Four strategies across your selected universe"><div className="grid-four"><Field label="Lookback days"><Input type="number" value={lookback} onChange={(e) => setLookback(e.target.value)} /></Field><Field label="Universe"><TickerInput value={tickers} onChange={setTickers} rows={4} /></Field><div className="field-end"><Toggle checked={includeSell} onChange={setIncludeSell} label="Include sell strategies" /><Button onClick={() => result.run()} loading={result.loading}>Scan signals</Button></div></div></Panel>{result.error && <Alert kind="error">{result.error}</Alert>}<div className="stat-grid"><Stat label="Signals found" value={fmt(signals.length)} detail="across selected tickers" tone="green" /><Stat label="Buy signals" value={fmt(signals.filter((s) => s.signal_type === 'BUY').length)} tone="green" /><Stat label="Sell signals" value={fmt(signals.filter((s) => s.signal_type === 'SELL').length)} tone="red" /><Stat label="Position plans" value={fmt(plans.length)} detail="within capital limit" /></div><div className="grid-two"><Panel title="Signal log" subtitle={result.value?.errors?.length ? `${result.value.errors.length} ticker errors` : 'Latest strategy output'}><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'signal_type', label: 'Type', render: (r) => <span className={r.signal_type === 'BUY' ? 'text-green' : 'text-red'}>{r.signal_type}</span> }, { key: 'strategy', label: 'Strategy' }, { key: 'price', label: 'Price', render: (r) => yen(r.price) }, { key: 'stop_loss', label: 'Stop', render: (r) => yen(r.stop_loss) }, { key: 'date', label: 'Date', render: (r) => date(r.date) }]} rows={signals} empty="No signals found in this period." /></Panel><Panel title="Position sizing" subtitle="Risk-managed plans"><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'shares', label: 'Shares', render: (r) => fmt(r.shares) }, { key: 'entry_price', label: 'Entry', render: (r) => yen(r.entry_price) }, { key: 'stop_loss', label: 'Stop', render: (r) => yen(r.stop_loss) }, { key: 'risk_amount', label: 'Risk', render: (r) => yen(r.risk_amount) }]} rows={plans} empty="No valid position plans." /></Panel></div></>
}

function BacktestView({ settings }) {
  const [ticker, setTicker] = useState('7203'); const [strategy, setStrategy] = useState('VolumeBreakout'); const [capital, setCapital] = useState(settings.capital); const [lookback, setLookback] = useState(730); const [result, setResult] = useState(null); const [loading, setLoading] = useState(false); const [error, setError] = useState('')
  async function run() { setLoading(true); setError(''); try { setResult(await api.backtest({ ticker, strategy, initial_capital: +capital, lookback_days: +lookback, risk_per_trade: settings.risk_percent / 100, hard_stop_pct: settings.hard_stop_percent / 100, max_holding_days: 60, take_profit_pct: 0, commission_pct: 0.001, slippage_pct: 0.001 })) } catch (e) { setError(e.message) } finally { setLoading(false) } }
  const trades = result?.trades || []
  return <><SectionTitle kicker="RESEARCH LAB">Strategy backtest</SectionTitle><Panel title="Simulation settings" subtitle="Signals fill at the next bar open"><div className="grid-four"><Field label="Ticker"><Input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} /></Field><Field label="Strategy"><Select value={strategy} onChange={(e) => setStrategy(e.target.value)}><option>VolumeBreakout</option><option>PullbackMA</option></Select></Field><Field label="Initial capital"><Input type="number" value={capital} onChange={(e) => setCapital(e.target.value)} /></Field><Field label="Lookback days"><Input type="number" value={lookback} onChange={(e) => setLookback(e.target.value)} /></Field></div><div className="panel-actions"><Button onClick={run} loading={loading}>Run backtest</Button></div></Panel>{error && <Alert kind="error">{error}</Alert>}{result && <><div className="stat-grid"><Stat label="Total return" value={pct(result.total_return_pct)} tone={result.total_return_pct >= 0 ? 'green' : 'red'} /><Stat label="Win rate" value={pct(result.win_rate)} /><Stat label="Sharpe ratio" value={fmt(result.sharpe_ratio, 2)} /><Stat label="Max drawdown" value={pct(result.max_drawdown_pct)} tone="red" /><Stat label="Total trades" value={fmt(result.total_trades)} /><Stat label="Profit factor" value={fmt(result.profit_factor, 2)} /></div><Panel title="Equity curve" subtitle={`${result.start_date} → ${result.end_date}`}><EquityChart values={result.equity_curve || []} /></Panel><Panel title="Trade log"><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'strategy', label: 'Strategy' }, { key: 'entry_price', label: 'Entry', render: (r) => yen(r.entry_price) }, { key: 'exit_price', label: 'Exit', render: (r) => yen(r.exit_price) }, { key: 'pnl', label: 'P/L', render: (r) => <span className={r.pnl >= 0 ? 'text-green' : 'text-red'}>{yen(r.pnl)}</span> }, { key: 'exit_reason', label: 'Exit' }]} rows={trades} empty="No completed trades." /></Panel></>}</>
}
function EquityChart({ values }) { if (!values.length) return <div className="empty">No equity points</div>; const w=900,h=220,pad=25; const min=Math.min(...values), max=Math.max(...values), range=max-min||1; const points=values.map((v,i)=>`${pad+i*(w-pad*2)/Math.max(values.length-1,1)},${h-pad-(v-min)*(h-pad*2)/range}`).join(' '); return <div className="equity-wrap"><svg className="equity-chart" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Equity curve"><line x1={pad} x2={w-pad} y1={h-pad} y2={h-pad} className="grid-line" /><polyline points={points} className="equity-line" /></svg></div> }

function PortfolioView({ settings }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [form, setForm] = useState({ ticker: '', shares: 100, entry_price: 0, stop_loss: 0, strategy: 'manual', sector: '' })
  const load = async () => {
    setLoading(true); setError('')
    try { setData(await api.portfolio()) } catch (e) { setError(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])
  const positions = data?.positions || []; const closed = data?.closed_trades || []; const stats = data?.stats || {}
  async function action(fn) {
    setLoading(true); setError('')
    try { await fn(); await load() } catch (e) { setError(e.message) } finally { setLoading(false) }
  }
  async function addPosition(event) {
    event.preventDefault(); setLoading(true); setError('')
    try { await api.addPosition({ ...form, shares: +form.shares, entry_price: +form.entry_price, stop_loss: +form.stop_loss }); setForm({ ticker: '', shares: 100, entry_price: 0, stop_loss: 0, strategy: 'manual', sector: '' }); await load() } catch (e) { setError(e.message) } finally { setLoading(false) }
  }
  async function closePosition(row) {
    const raw = window.prompt('Exit price', String(row.current_price || row.entry_price))
    if (raw === null) return
    const exitPrice = Number(raw)
    if (!Number.isFinite(exitPrice) || exitPrice <= 0) { setError('Enter a valid exit price'); return }
    if (!window.confirm(`Close ${row.ticker} at ${yen(exitPrice)}?`)) return
    await action(() => api.closePosition(row.ticker, { exit_price: exitPrice, reason: 'MANUAL' }))
  }
  return <><SectionTitle kicker="RISK CONTROL">Portfolio tracker</SectionTitle><div className="panel-actions"><Button onClick={load} loading={loading}>Refresh view</Button><Button variant="secondary" onClick={() => action(api.refreshPrices)}>Update market data</Button><Button variant="secondary" onClick={() => action(() => api.checkPortfolio({ refresh_prices: true, send_alerts: false }))}>Check all levels</Button></div>{error && <Alert kind="error">{error}</Alert>}<Panel title="Add position" subtitle="Enter a risk-defined position; the backend stores it per account"><form onSubmit={addPosition}><div className="form-grid"><Field label="Ticker"><Input required value={form.ticker} onChange={(e) => setForm({ ...form, ticker: e.target.value.toUpperCase() })} /></Field><Field label="Shares"><Input required type="number" min="1" value={form.shares} onChange={(e) => setForm({ ...form, shares: e.target.value })} /></Field><Field label="Entry price"><Input required type="number" min="0.01" step="0.01" value={form.entry_price} onChange={(e) => setForm({ ...form, entry_price: e.target.value })} /></Field><Field label="Stop price"><Input required type="number" min="0.01" step="0.01" value={form.stop_loss} onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} /></Field><Field label="Strategy"><Input value={form.strategy} onChange={(e) => setForm({ ...form, strategy: e.target.value })} /></Field><Field label="Sector"><Input value={form.sector} onChange={(e) => setForm({ ...form, sector: e.target.value })} /></Field></div><Button type="submit" loading={loading}>Add position</Button></form></Panel><div className="stat-grid"><Stat label="Open positions" value={fmt(stats.open_positions)} /><Stat label="Market value" value={yen(stats.total_market_value)} /><Stat label="Unrealized P/L" value={yen(stats.total_unrealized_pnl)} tone={stats.total_unrealized_pnl >= 0 ? 'green' : 'red'} /><Stat label="Win rate" value={stats.closed_trades ? pct(stats.win_rate) : '—'} /></div><Panel title="Open positions" subtitle="Risk limits and execution levels"><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'shares', label: 'Shares', render: (r) => fmt(r.shares) }, { key: 'entry_price', label: 'Entry', render: (r) => yen(r.entry_price) }, { key: 'current_price', label: 'Current', render: (r) => r.current_price ? yen(r.current_price) : '—' }, { key: 'unrealized_pnl', label: 'P/L', render: (r) => <span className={r.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}>{yen(r.unrealized_pnl)}</span> }, { key: 'stop_loss', label: 'Stop', render: (r) => yen(r.stop_loss) }, { key: 'sector', label: 'Sector' }, { key: 'actions', label: 'Actions', render: (r) => <div className="inline-controls"><Button variant="secondary" onClick={() => action(() => api.enableTrailing(r.ticker, { trail_pct: 0.05 }))}>Trail</Button><Button variant="secondary" onClick={() => action(() => api.recalculateTargets(r.ticker))}>Targets</Button><Button variant="danger" onClick={() => closePosition(r)}>Close</Button></div> }]} rows={positions} empty="No open positions." /></Panel><Panel title="Closed trades" subtitle="Realized history"><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'shares', label: 'Shares' }, { key: 'pnl', label: 'P/L', render: (r) => <span className={r.pnl >= 0 ? 'text-green' : 'text-red'}>{yen(r.pnl)}</span> }, { key: 'pnl_pct', label: 'Return', render: (r) => pct(r.pnl_pct) }, { key: 'exit_date', label: 'Exit date' }, { key: 'reason', label: 'Reason' }]} rows={closed} empty="No closed trades." /></Panel></>
}

function EarningsView() { const [tickers, setTickers] = useState(TICKER_DEFAULTS.slice(0, 5).join('\n')); const [days, setDays] = useState(14); const [data, setData] = useState(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(''); async function run() { setLoading(true); setError(''); try { setData(await api.earnings({ tickers: normalizeTickers(tickers), warning_days: +days })) } catch (e) { setError(e.message) } finally { setLoading(false) } } const items = Object.entries(data?.results || {}); return <><SectionTitle kicker="EVENT RISK">Earnings calendar</SectionTitle><Panel title="Upcoming events" subtitle="Flag high-risk positions before announcements"><div className="grid-four"><Field label="Warning window (days)"><Input type="number" value={days} onChange={(e) => setDays(e.target.value)} /></Field><Field label="Tickers"><TickerInput value={tickers} onChange={setTickers} rows={4} /></Field><div className="field-end"><Button onClick={run} loading={loading}>Check earnings</Button></div></div></Panel>{error && <Alert kind="error">{error}</Alert>}{data && <><div className="stat-grid"><Stat label="Safe" value={fmt(data.safe?.length)} tone="green" /><Stat label="Risky" value={fmt(data.risky?.length)} tone="red" /></div><Panel title="Calendar results"><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'next_earnings_date', label: 'Next event', render: (r) => date(r.next_earnings_date) }, { key: 'days_until_earnings', label: 'Days', render: (r) => fmt(r.days_until_earnings) }, { key: 'is_upcoming', label: 'Risk', render: (r) => r.is_upcoming ? <span className="badge badge-red">High</span> : <span className="badge badge-green">Clear</span> }]} rows={items.map(([ticker, r]) => ({ ticker, ...r }))} /></Panel></>}</> }

function SmartView({ settings }) { const [tickers, setTickers] = useState(TICKER_DEFAULTS.join('\n')); const [topN, setTopN] = useState(10); const [result, setResult] = useState(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(''); async function run() { setLoading(true); setError(''); try { setResult(await api.smartScreen({ tickers: normalizeTickers(tickers), top_n: +topN, lookback_days: settings.lookback_days, min_roe: 0.08, max_pe: 20, max_pb: 3, min_dividend_yield: 0.005 })) } catch (e) { setError(e.message) } finally { setLoading(false) } } return <><SectionTitle kicker="MULTI-PASS">Smart screen</SectionTitle><Panel title="Quality ranking" subtitle="Fundamentals → technical trend → weighted score"><div className="grid-four"><Field label="Top results"><Input type="number" value={topN} onChange={(e) => setTopN(e.target.value)} /></Field><Field label="Ticker universe"><TickerInput value={tickers} onChange={setTickers} rows={5} /></Field><div className="field-end"><Button onClick={run} loading={loading}>Run smart screen</Button></div></div></Panel>{error && <Alert kind="error">{error}</Alert>}<Panel title="Ranked candidates" subtitle="Scores normalize missing factors conservatively"><Table columns={[{ key: 'rank', label: '#' }, { key: 'ticker', label: 'Ticker' }, { key: 'score', label: 'Score', render: (r) => fmt(r.score, 3) }, { key: 'fundamental_score', label: 'Fundamental', render: (r) => fmt(r.fundamental_score, 3) }, { key: 'technical_score', label: 'Technical', render: (r) => fmt(r.technical_score, 3) }, { key: 'sector', label: 'Sector' }]} rows={result?.results || []} empty="Run the screen to rank candidates." /></Panel></> }

function MtfView() { const [tickers, setTickers] = useState(TICKER_DEFAULTS.slice(0, 5).join('\n')); const [confidence, setConfidence] = useState(70); const [result, setResult] = useState(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(''); async function run() { setLoading(true); setError(''); try { setResult(await api.mtfScan({ tickers: normalizeTickers(tickers), min_confidence: +confidence / 100, lookback_days: 545 })) } catch (e) { setError(e.message) } finally { setLoading(false) } } return <><SectionTitle kicker="CONFLUENCE">Multi-timeframe confirmation</SectionTitle><Panel title="Daily + weekly alignment" subtitle="Increase conviction by requiring trend agreement"><div className="grid-four"><Field label="Min confidence (%)"><Input type="number" value={confidence} onChange={(e) => setConfidence(e.target.value)} /></Field><Field label="Tickers"><TickerInput value={tickers} onChange={setTickers} rows={4} /></Field><div className="field-end"><Button onClick={run} loading={loading}>Run MTF scan</Button></div></div></Panel>{error && <Alert kind="error">{error}</Alert>}<Panel title="Confirmed signals"><Table columns={[{ key: 'ticker', label: 'Ticker' }, { key: 'strategy', label: 'Strategy' }, { key: 'entry_price', label: 'Entry', render: (r) => yen(r.entry_price) }, { key: 'stop_loss', label: 'Stop', render: (r) => yen(r.stop_loss) }, { key: 'confidence', label: 'Confidence', render: (r) => <span className="confidence"><i style={{ width: `${r.confidence * 100}%` }} />{pct(r.confidence, 0)}</span> }, { key: 'weekly_confirmed', label: 'Weekly', render: (r) => r.weekly_confirmed ? 'Confirmed' : 'Daily only' }]} rows={result?.signals || []} empty="No confirmed signals." /></Panel></> }

function ProfitView() { const [rows, setRows] = useState([{ ticker: '7203', entry_price: 2000, target_pct: 5, shares: 100 }]); const [summary, setSummary] = useState(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(''); const [dirty, setDirty] = useState(false); const saveQueue = useRef(Promise.resolve()); const saveGeneration = useRef(0); useEffect(() => { api.getTargetRows().then((data) => { if (data?.rows?.length) setRows(data.rows); else if (Array.isArray(data)) setRows(data) }).catch(() => {}) }, []); async function save(next = rows) { setRows(next); const generation = ++saveGeneration.current; saveQueue.current = saveQueue.current.catch(() => {}).then(async () => { await api.saveTargetRows(next); if (generation === saveGeneration.current) setDirty(false); setSummary(await api.summarizeProfitTargets({ rows: next })) }); try { await saveQueue.current } catch (e) { setError(e.message) } } function update(i, key, value) { const next = rows.map((r, n) => n === i ? { ...r, [key]: value } : r); setRows(next); saveGeneration.current += 1; setDirty(true) } async function calculate() { setLoading(true); setError(''); try { setSummary(await api.summarizeProfitTargets({ rows })) } catch (e) { setError(e.message) } finally { setLoading(false) } } return <><SectionTitle kicker="PLANNING">Profit target calculator</SectionTitle><Panel title="Position targets" subtitle="Model exit prices and portfolio-level profit"><div className="target-table"><div className="target-row target-head"><span>Ticker</span><span>Entry price</span><span>Shares</span><span>Target %</span><span>Exit price</span><span /></div>{rows.map((row, i) => <div className="target-row" key={i}><Input value={row.ticker} onChange={(e) => update(i, 'ticker', e.target.value.toUpperCase())} /><Input type="number" value={row.entry_price} onChange={(e) => update(i, 'entry_price', +e.target.value)} /><Input type="number" value={row.shares} onChange={(e) => update(i, 'shares', +e.target.value)} /><Input type="number" value={row.target_pct} onChange={(e) => update(i, 'target_pct', +e.target.value)} /><strong>{row.entry_price > 0 ? yen(row.entry_price * (1 + row.target_pct / 100)) : '—'}</strong><button className="icon-btn" onClick={() => save(rows.filter((_, n) => n !== i))}>×</button></div>)}</div><div className="panel-actions"><Button onClick={() => save([...rows, { ticker: '', entry_price: 0, target_pct: 5, shares: 0 }])}>Add position</Button><Button onClick={() => save()} disabled={!dirty}>Save rows</Button><Button variant="secondary" onClick={calculate} loading={loading}>Calculate summary</Button><Button variant="ghost" onClick={() => save([])}>Clear all</Button></div></Panel>{error && <Alert kind="error">{error}</Alert>}{summary && <div className="stat-grid"><Stat label="Positions" value={fmt(summary.position_count)} /><Stat label="Total cost" value={yen(summary.total_invested)} /><Stat label="Target value" value={yen(summary.total_target_value)} /><Stat label="Expected profit" value={yen(summary.total_profit)} tone={summary.total_profit >= 0 ? 'green' : 'red'} /><Stat label="Weighted target" value={`${Number(summary.weighted_target_pct || 0).toFixed(2)}%`} /></div>}</> }

function PriceTargetView({ settings }) { const [ticker, setTicker] = useState('7203'); const [lookback, setLookback] = useState(365); const [entry, setEntry] = useState(0); const [data, setData] = useState(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(''); async function run() { setLoading(true); setError(''); try { setData(await api.priceTargets({ ticker, lookback_days: +lookback, entry_price: +entry || null, hard_stop_pct: settings.hard_stop_percent / 100 })) } catch (e) { setError(e.message) } finally { setLoading(false) } } return <><SectionTitle kicker="LEVELS">Price target analysis</SectionTitle><Panel title="Buy and sell zones" subtitle="Fibonacci, support/resistance, and risk/reward"><div className="grid-four"><Field label="Ticker"><Input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} /></Field><Field label="Lookback days"><Input type="number" value={lookback} onChange={(e) => setLookback(e.target.value)} /></Field><Field label="Entry price (optional)"><Input type="number" value={entry} onChange={(e) => setEntry(e.target.value)} /></Field><div className="field-end"><Button onClick={run} loading={loading}>Analyze</Button></div></div></Panel>{error && <Alert kind="error">{error}</Alert>}{data && <><div className="stat-grid"><Stat label="Current price" value={yen(data.current_price)} /><Stat label="Suggested entry" value={yen(data.buy_zone?.entry_suggestion)} tone="green" /><Stat label="Suggested exit" value={yen(data.sell_zone?.exit_suggestion)} tone="red" /><Stat label="R:R ratio" value={fmt(data.risk_reward?.ratio, 2)} /></div><div className="grid-two"><Panel title="Buy zone" subtitle={data.buy_zone?.reasoning}><div className="zone"><span>Zone low</span><strong>{yen(data.buy_zone?.zone_low)}</strong><span>Zone high</span><strong>{yen(data.buy_zone?.zone_high)}</strong></div></Panel><Panel title="Sell zone" subtitle={data.sell_zone?.reasoning}><div className="zone"><span>Zone low</span><strong>{yen(data.sell_zone?.zone_low)}</strong><span>Zone high</span><strong>{yen(data.sell_zone?.zone_high)}</strong></div></Panel></div><Panel title="Technical levels"><div className="levels"><div><h3>Support</h3>{(data.sr?.supports || []).map((x) => <span key={x}>{yen(x)}</span>)}</div><div><h3>Resistance</h3>{(data.sr?.resistances || []).map((x) => <span key={x}>{yen(x)}</span>)}</div><div><h3>Take profits</h3>{(data.take_profits || []).map((x, i) => <span key={x}>TP{i + 1} · {yen(x)}</span>)}</div></div></Panel></>}</> }

function AlertsView({ user }) { const [channels, setChannels] = useState(null); const [tickers, setTickers] = useState(TICKER_DEFAULTS.join('\n')); const [scan, setScan] = useState(null); const [loading, setLoading] = useState(false); const [sendAlerts, setSendAlerts] = useState(false); const [message, setMessage] = useState(''); useEffect(() => { api.alertChannels().then(setChannels).catch(() => {}) }, []); async function test(channel) { try { const r = await api.testAlert(channel); setMessage(r.sent ? 'Test sent successfully' : 'Test could not be delivered') } catch (e) { setMessage(e.message) } } async function run() { setLoading(true); setMessage(''); try { if (sendAlerts && !window.confirm('Broadcast this scan to configured channels?')) return; setScan(await api.alertScan({ tickers: normalizeTickers(tickers), lookback_days: 365, send_alerts: sendAlerts })) } catch (e) { setMessage(e.message) } finally { setLoading(false) } } return <><SectionTitle kicker="NOTIFICATIONS">Signal alerts</SectionTitle><div className="grid-two"><Panel title="Channels" subtitle="Global delivery configuration"><div className="channel-row"><div><strong>Telegram</strong><small>{channels?.telegram?.configured ? 'Configured' : 'Not configured'}</small></div><span className={channels?.telegram?.configured ? 'badge badge-green' : 'badge badge-muted'}>{channels?.telegram?.configured ? 'Ready' : 'Off'}</span><Button variant="secondary" disabled={!user?.can_send_alerts} onClick={() => test('telegram')}>Test</Button></div><div className="channel-row"><div><strong>Slack</strong><small>{channels?.slack?.configured ? 'Configured' : 'Not configured'}</small></div><span className={channels?.slack?.configured ? 'badge badge-green' : 'badge badge-muted'}>{channels?.slack?.configured ? 'Ready' : 'Off'}</span><Button variant="secondary" disabled={!user?.can_send_alerts} onClick={() => test('slack')}>Test</Button></div>{!user?.can_send_alerts && <p className="muted">Your account cannot send external alerts.</p>}</Panel><Panel title="Manual scan" subtitle="Scan without broadcasting by default"><Field label="Tickers"><TickerInput value={tickers} onChange={setTickers} rows={6} /></Field><Toggle checked={sendAlerts} onChange={setSendAlerts} label="Broadcast results (admin only)" /><Button onClick={run} loading={loading} disabled={sendAlerts && !user?.can_send_alerts}>{sendAlerts ? 'Scan and broadcast' : 'Scan signals'}</Button>{scan && <p className="muted">{Object.values(scan.results || {}).flat().length} signals found{scan.delivery ? ` · delivered: ${Object.entries(scan.delivery).filter(([, ok]) => ok).map(([name]) => name).join(', ') || 'none'}` : ''}</p>}</Panel></div>{message && <Alert kind="info">{message}</Alert>}<Panel title="Delivery notes" subtitle="Use environment variables or the scheduled GitHub Action"><ul className="notes"><li>Telegram and Slack credentials are read from the backend environment.</li><li>Manual scans default to delivery off to prevent accidental broadcasts.</li><li>The existing workflow can run a daily scan at 15:30 JST.</li></ul></Panel></> }

function SettingsView({ settings, updateSettings, setSettings, resetSettings }) {
  const [autoScan, setAutoScan] = useState(null)
  const [passwords, setPasswords] = useState({ current_password: '', new_password: '' })
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  useEffect(() => { api.getAutoScan().then(setAutoScan).catch(() => setAutoScan({ enabled: false, interval_minutes: 30, lookback_days: 365, tickers: TICKER_DEFAULTS })) }, [])
  async function saveAutoScan() {
    setError(''); setNotice('')
    try { const next = await api.saveAutoScan({ ...autoScan, interval_minutes: +autoScan.interval_minutes, lookback_days: +autoScan.lookback_days, tickers: normalizeTickers(autoScan.tickers?.join?.('\n') || autoScan.tickers || '') }); setAutoScan(next); setNotice('Auto-scan settings saved') } catch (e) { setError(e.message) }
  }
  async function reset() {
    setError(''); setNotice('')
    try { const next = await resetSettings(); if (next) setSettings(normalizeSettings(next)); setNotice('Settings reset to defaults') } catch (e) { setError(e.message) }
  }
  async function changePassword(event) {
    event.preventDefault(); setError(''); setNotice('')
    try { await api.changePassword(passwords.current_password, passwords.new_password); setPasswords({ current_password: '', new_password: '' }); setNotice('Password changed; this session was refreshed') } catch (e) { setError(e.message) }
  }
  return <><SectionTitle kicker="WORKSPACE">Settings</SectionTitle><div className="grid-two"><Panel title="Risk preferences" subtitle="These values drive screening, signals, and portfolio sizing"><div className="form-grid"><Field label="Capital (JPY)"><Input type="number" min="100000" value={settings.capital} onChange={(e) => updateSettings({ capital: e.target.value })} /></Field><Field label="Risk per trade (%)"><Input type="number" min="0.1" max="100" step="0.1" value={settings.risk_percent} onChange={(e) => updateSettings({ risk_percent: e.target.value })} /></Field><Field label="Hard stop (%)"><Input type="number" min="0.1" max="99" step="0.1" value={settings.hard_stop_percent} onChange={(e) => updateSettings({ hard_stop_percent: e.target.value })} /></Field><Field label="Lookback days"><Input type="number" min="30" max="3650" value={settings.lookback_days} onChange={(e) => updateSettings({ lookback_days: e.target.value })} /></Field></div><Button variant="secondary" onClick={reset}>Reset defaults</Button></Panel><Panel title="Auto-scan" subtitle="Persist a schedule for the trusted server-side scanner"><div className="form-grid"><Toggle checked={autoScan?.enabled || false} onChange={(value) => setAutoScan({ ...(autoScan || {}), enabled: value })} label="Enable auto-scan" /><Field label="Interval (minutes)"><Input type="number" min="1" max="1440" value={autoScan?.interval_minutes || 30} onChange={(e) => setAutoScan({ ...(autoScan || {}), interval_minutes: e.target.value })} /></Field><Field label="Lookback days"><Input type="number" min="30" max="3650" value={autoScan?.lookback_days || 365} onChange={(e) => setAutoScan({ ...(autoScan || {}), lookback_days: e.target.value })} /></Field><Field label="Tickers"><TickerInput value={Array.isArray(autoScan?.tickers) ? autoScan.tickers.join('\n') : (autoScan?.tickers || TICKER_DEFAULTS.join('\n'))} onChange={(value) => setAutoScan({ ...(autoScan || {}), tickers: value.split(/[\s,]+/).filter(Boolean) })} rows={4} /></Field></div><Button onClick={saveAutoScan}>Save auto-scan</Button></Panel></div><Panel title="Account security" subtitle="Changing your password revokes all other sessions"><form onSubmit={changePassword}><div className="form-grid"><Field label="Current password"><Input required type="password" autoComplete="current-password" value={passwords.current_password} onChange={(e) => setPasswords({ ...passwords, current_password: e.target.value })} /></Field><Field label="New password"><Input required minLength="8" type="password" autoComplete="new-password" value={passwords.new_password} onChange={(e) => setPasswords({ ...passwords, new_password: e.target.value })} /></Field></div><Button type="submit">Change password</Button></form></Panel>{notice && <Alert kind="info">{notice}</Alert>}{error && <Alert kind="error">{error}</Alert>}</>
}

function GuideView() { const sections = [['SMA', 'Simple moving average over N periods identifies short, medium, and long-term trend.'], ['RSI', 'Relative Strength Index measures momentum and flags overbought or oversold conditions.'], ['ATR', 'Average True Range measures volatility and helps size stops to the market.'], ['Volume breakout', 'A close above the prior range with elevated volume and an SMA trend filter.'], ['Pullback MA', 'A recovery above a moving average inside a confirmed long-term uptrend.'], ['Risk management', 'The 1% rule limits capital at risk per trade; a hard stop caps downside.']]; return <><SectionTitle kicker="REFERENCE">Trading guide</SectionTitle><div className="guide-grid">{sections.map(([title, text]) => <article className="guide-card" key={title}><div className="guide-index">{String(sections.indexOf(sections.find((s) => s[0] === title)) + 1).padStart(2, '0')}</div><h2>{title}</h2><p>{text}</p><button className="text-btn">Read notes →</button></article>)}</div><Panel title="Operating principles" subtitle="A simple process beats a complex one"><div className="principles"><div><strong>01</strong><span>Define the trade setup before entering.</span></div><div><strong>02</strong><span>Size the position from the stop distance.</span></div><div><strong>03</strong><span>Record the outcome and review the process.</span></div></div></Panel></> }

createRoot(document.getElementById('root')).render(<App />)
