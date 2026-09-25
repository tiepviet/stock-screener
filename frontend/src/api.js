const API_BASE = import.meta.env.VITE_API_URL || '/api/v1'
const TOKEN_KEY = 'tse_jwt'

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

export function getToken() {
  return window.localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) window.localStorage.setItem(TOKEN_KEY, token)
  else window.localStorage.removeItem(TOKEN_KEY)
}

function formatApiDetail(detail) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (!item || typeof item !== 'object') return String(item)
      const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : item.loc
      return `${field || 'request'}: ${item.msg || 'Invalid value'}`
    }).join('; ')
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return ''
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  const token = getToken()
  const requestToken = token
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

  let response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch (error) {
    throw new ApiError('Không thể kết nối tới máy chủ. Kiểm tra backend đang chạy.', 0, error)
  }

  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json')
    ? await response.json().catch(() => null)
    : await response.text()

  if (!response.ok) {
    const detail = formatApiDetail(payload?.detail || payload?.message) || (typeof payload === 'string' ? payload : null)
    if (response.status === 401 && requestToken && getToken() === requestToken) {
      setToken(null)
      window.dispatchEvent(new Event('tse-auth-expired'))
    }
    throw new ApiError(detail || `Request failed (${response.status})`, response.status, payload)
  }
  return payload
}

const json = (method, body) => ({ method, body: JSON.stringify(body) })
const post = (path, body) => request(path, json('POST', body))
const put = (path, body) => request(path, json('PUT', body))
const patch = (path, body) => request(path, json('PATCH', body))
const del = (path) => request(path, { method: 'DELETE' })

const qs = (params = {}) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value)
  })
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  health: () => request('/health'),
  setupStatus: () => request('/auth/setup-status'),
  login: (username, password) => post('/auth/login', { username, password }),
  me: () => request('/auth/me'),
  logout: () => post('/auth/logout'),
  changePassword: async (currentPassword, newPassword) => {
    const result = await post('/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    })
    if (result?.access_token) setToken(result.access_token)
    return result
  },

  getSettings: () => request('/me/settings/sidebar'),
  saveSettings: (settings) => put('/me/settings/sidebar', settings),
  resetSettings: () => post('/me/settings/sidebar/reset'),
  getChartSettings: () => request('/me/settings/chart'),
  saveChartSettings: (settings) => put('/me/settings/chart', settings),
  getTargetRows: () => request('/me/profit-target-rows').then((data) => data.rows || data.target_rows || data),
  saveTargetRows: (rows) => put('/me/profit-target-rows', { rows }),
  getWatchlist: () => request('/me/watchlist').then((data) => data.tickers || data.watchlist || data),
  addWatchlist: (tickers) => post('/me/watchlist', { tickers }),
  removeWatchlist: (ticker) => del(`/me/watchlist/${encodeURIComponent(ticker)}`),

  defaults: () => request('/markets/defaults'),
  chart: (ticker, params) => request(`/markets/${encodeURIComponent(ticker)}/ohlcv${qs(params)}`),
  fundamentals: (ticker) => request(`/markets/${encodeURIComponent(ticker)}/fundamentals`),
  fundamentalScreen: (body) => post('/screeners/fundamental', body),
  smartScreen: (body) => post('/screeners/smart', body),
  signalScan: (body) => post('/signals/scans', body),
  mtfScan: (body) => post('/signals/multi-timeframe', body),
  backtest: (body) => post('/backtests', body),
  earnings: (body) => post('/earnings/checks', body),
  priceTargets: (body) => post('/price-targets/analyze', body),
  calculateProfitTargets: (body) => post('/profit-targets/calculate', body),
  summarizeProfitTargets: (body) => post('/profit-targets/summarize', body),

  portfolio: () => request('/portfolio'),
  addPosition: (body) => post('/portfolio/positions', body),
  closePosition: (ticker, body) => post(`/portfolio/positions/${encodeURIComponent(ticker)}/close`, body),
  enableTrailing: (ticker, body) => patch(`/portfolio/positions/${encodeURIComponent(ticker)}/trailing-stop`, body),
  recalculateTargets: (ticker) => post(`/portfolio/positions/${encodeURIComponent(ticker)}/recalculate-targets`),
  refreshPrices: () => post('/portfolio/refresh-prices'),
  checkPortfolio: (body = {}) => post('/portfolio/check', body),

  alertChannels: () => request('/alerts/channels'),
  testAlert: (channel) => post(`/alerts/channels/${channel}/test`),
  alertScan: (body) => post('/alerts/scans', body),
  getAutoScan: () => request('/me/auto-scan'),
  saveAutoScan: (body) => put('/me/auto-scan', body),
  deleteAutoScan: () => del('/me/auto-scan'),
}
