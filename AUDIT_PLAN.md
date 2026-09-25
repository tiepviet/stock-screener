# TSE Stock Screener Audit and Remediation Plan

**Audit date:** 2026-09-25
**Baseline:** `f36dd9a` (`main` / `origin/main`)
**Reviewed artifact:** current dirty working tree, including untracked migration files
**Overall status:** **BLOCKED for public production release**
**Production-readiness score:** **40/100**

This document records the consolidated audit, verified findings, remediation order, and release gates for the FastAPI + React migration. It is a plan and evidence record, not a claim that the listed defects are fixed.

## 1. Scope and evidence baseline

The review used:

- `README.md`
- `design-system/tse-screener/MASTER.md`
- source and test docstrings
- `src/stock_screener/`
- `backend/`
- `frontend/`
- `tests/`
- `pyproject.toml` and `requirements.txt`
- `render.yaml`
- GitHub Actions workflows
- current Git status and history
- local test, build, audit, compile, and synthetic probes

There is no formal PRD or roadmap. Completion estimates are therefore observational and based on the documented feature claims and observed behavior.

## 2. Verification snapshot

| Check | Result |
|---|---|
| `/usr/bin/python3 -m pytest -q` | 233 passed, 1 warning |
| Python runtime used locally | 3.9.6, below the declared `>=3.12` requirement |
| Declared dependency floors | Not exercised locally |
| `npm run build --prefix frontend` | Passed with Vite 8.3.1 |
| `npm audit --omit=dev` | 0 vulnerabilities |
| Full `npm audit` | 0 vulnerabilities |
| `python3 -m compileall -q backend src tests` | Passed |
| `git diff --check` | Passed |
| Ruff, mypy, coverage | Not run locally |
| Live yfinance provider | Not verified |
| Telegram and Slack delivery | Not verified against real services |
| Render deployment | Not verified |
| Browser/E2E flow | Not verified |

The working tree changed during the audit. Findings below refer to the latest snapshot read before this plan was created. Re-run the verification suite after each remediation phase.

## 3. Current strengths

The following controls are present and should be preserved:

- FastAPI authentication with bearer JWT validation and database-backed token revocation.
- bcrypt password hashing with a 72-byte password limit.
- Strict Pydantic request models, finite-number checks, bounded lists, and validation-error redaction.
- CORS restrictions, CSP/security headers, HSTS configuration, and request-body limits.
- Login, API, provider, and alert rate limits with bounded in-process state.
- Provider concurrency and minimum-interval controls.
- User-scoped SQLite settings and portfolio files.
- Atomic portfolio writes, file/process locks, position bounds, and corrupt-file quarantine.
- Persisted alert capability checks for delivery and auto-scan.
- Portfolio alert channel claims and failed-channel retry state.
- Persistent Render disk, health check, one-worker configuration, and pinned Node/Python versions.
- React build succeeds and the current npm dependency audit is clean.
- Streamlit has been moved to an explicitly unsupported `legacy/` reference directory.

## 4. Release blockers

### R-001: Runtime and CI artifacts are untracked

**Severity:** P0 / release blocker
**Evidence:** `git status` shows `backend/`, `frontend/`, `legacy/`, `.github/workflows/ci.yml`, and multiple tests as untracked. `git ls-tree HEAD` contains none of those paths. `render.yaml:6-7` requires the untracked backend and frontend lockfile.

**Impact:** A clean checkout cannot build or start the current migration. Render and CI cannot reproduce the reviewed artifact.

**Acceptance criteria:**

- All runtime, frontend, lockfile, CI, legacy-reference, and test files are tracked.
- A clean clone passes the documented build and startup commands.
- `git archive HEAD` contains every runtime input.
- No ignored secrets, databases, caches, `node_modules`, or build output are committed.

### R-002: Python test dependency is undeclared

**Severity:** P1
**Evidence:** API tests import `fastapi.testclient.TestClient`; `httpx` is absent from `requirements.txt` and the `dev` extra in `pyproject.toml`. The intended CI installs `requirements.txt ruff pytest`.

**Impact:** A clean CI environment can fail during test collection or TestClient initialization.

**Acceptance criteria:**

- Add a pinned-compatible `httpx` test dependency.
- Install the documented development extra in CI.
- Run the full API/security suite from a clean Python 3.12 environment.

## 5. Trading and backtest findings

### B-001: Entry-bar exits are omitted

**Severity:** P1 / High
**Evidence:** `src/stock_screener/backtest.py:206-285`. Exits are evaluated before the current bar entry is created.

**Impact:** A position filled at the current bar open cannot hit its stop or target until the next bar. A synthetic entry-bar stop/target probe ended at `END_OF_DATA` instead of exiting.

**Fix:** Evaluate the entry bar after the fill, or explicitly document and test a next-bar-only model.

**Acceptance criteria:**

- Entry-bar stop, target, gap-down, and same-bar ambiguity tests exist.
- Trade logs prove the execution convention.

### B-002: Commission is charged once and against entry notional

**Severity:** P1 / High
**Evidence:** `src/stock_screener/backtest.py:327-334`. The entry path does not charge commission; the close path subtracts `commission_pct * entry_price * shares` only.

**Impact:** P/L, capital, profit factor, average returns, and Sharpe are optimistic.

**Fix:** Charge both entry and exit notionals, including the configured slippage convention.

**Acceptance criteria:**

- Exact round-trip fee regression test.
- Entry and exit fees are visible in trade-level accounting.
- Flat, winning, losing, stop, target, and end-of-data trades are covered.

### B-003: Maximum drawdown percentage uses the wrong denominator

**Severity:** P1 / High
**Evidence:** `src/stock_screener/backtest.py:366-370`.

**Impact:** `[500, 100, 1000]` reports 40% drawdown instead of the correct 80% drawdown at the trough.

**Fix:** Compute pointwise percentage drawdown against the running peak and take the maximum magnitude.

### B-004: Risk sizing is a one-share floor, not a ceiling

**Severity:** P1 / High
**Evidence:** `src/stock_screener/risk_management.py:103-137`.

**Impact:** One share can be admitted even when it exceeds the configured risk budget or available capital.

**Fix:** Return a zero-share plan when one share cannot fit both constraints. Apply the same rule in the backtester.

### B-005: Weekly holding and Sharpe calculations are wrong

**Severity:** P1 for direct/domain use; latent for the current daily HTTP backtest route
**Evidence:** `src/stock_screener/backtest.py:195-210, 236-242, 359-364`.

**Impact:** Weekly bars interpret a 60-day setting as roughly 60 weeks, and Sharpe always annualizes as daily.

**Fix:** Define holding limits as calendar days or bars, detect frequency robustly, and annualize by interval.

### B-006: Historical signals become stale position plans

**Severity:** P1 / High
**Evidence:** `backend/services.py:237-283`, `src/stock_screener/risk_management.py:152-190`.

**Impact:** Multiple historical signals for one ticker consume multiple allocations and are displayed as current plans.

**Fix:** Separate historical logs from actionable plans, select the latest eligible signal, and include signal identity/date in the plan.

### B-007: MTF confirmation uses future weekly state

**Severity:** P1 / High
**Evidence:** `src/stock_screener/multi_timeframe.py:108-137`.

**Impact:** Historical daily signals are scored using the final weekly trend. SELL metadata can say `weekly_confirmed=False` even when a bearish weekly trend supplied the confidence bonus. Empty weekly data is represented as `False` and can be treated as bearish.

**Fix:** Resolve weekly state as of each signal date and use an explicit `bullish/bearish/unknown` state.

## 6. Data, earnings, and alert findings

### D-001: YFinance end date is treated as inclusive

**Severity:** P1 / High
**Evidence:** `src/stock_screener/data_loader.py:350-355`. Callers pass today's JST date directly to `yf.download`, whose `end` parameter is exclusive.

**Impact:** Post-close scans can omit the current session, including the current weekly bar.

**Fix:** Convert the public inclusive date to the provider's exclusive boundary and add a JST boundary test.

### D-002: Alert scans replay historical signals

**Severity:** P1 / High
**Evidence:** `src/stock_screener/alert.py:241-275, 331-393`; no seen-state or signal identity is stored.

**Impact:** Daily and scheduled scans resend old signals. Signal dates are omitted from the report.

**Fix:** Filter to the latest completed bar or store durable event keys containing ticker, signal date, strategy, and scan type.

### D-003: Alert messages have no size bound

**Severity:** P1 / High
**Evidence:** `format_summary_report()` has no cap or splitting. A synthetic 100-signal report was 5,777 characters, above Telegram's 4,096-character limit.

**Impact:** Large scans fail delivery, and a failed HTTP response is reduced to a boolean with limited diagnostic context.

**Fix:** Cap, chunk, or summarize messages and return per-channel failure details.

### D-004: Scheduler and GitHub workflow can both broadcast

**Severity:** P1 / High
**Evidence:** `backend/scheduler.py:127-149` and `.github/workflows/daily-scan.yml:22-27` are independent delivery paths.

**Impact:** Enabling auto-scan can produce duplicate global broadcasts.

**Fix:** Choose one authoritative scheduler, or use a shared durable idempotency key and delivery ledger.

### D-005: Earnings failures fail open

**Severity:** P1 / High
**Evidence:** `src/stock_screener/earnings_calendar.py:122-142, 168-197`. Provider errors become default `EarningsInfo`; `filter_safe()` classifies non-upcoming values as safe. Signal and alert paths do not invoke the calendar.

**Impact:** A provider outage can appear to confirm that a trade is safe.

**Fix:** Add explicit status/error state, fail closed for trading decisions, integrate earnings checks into signal admission, and test unknown states.

### D-006: React displays unknown earnings as Clear

**Severity:** P1 / High
**Evidence:** `frontend/src/main.jsx:252`. The component ignores the API `unknown` list and renders every non-upcoming item as a green Clear badge.

**Fix:** Render a distinct Unknown state and prevent unknown results from being presented as safe.

## 7. Portfolio and target findings

### P-001: Manual portfolio admission bypasses risk controls

**Severity:** P1 / High
**Evidence:** `backend/services.py:435-455` and `src/stock_screener/portfolio.py:665-730`.

**Impact:** Client-supplied shares, stop, and sector can exceed capital and risk limits. Probe: a 100,000 position was accepted by a tracker configured with 100 capital.

**Fix:** Enforce aggregate capital, per-trade risk, total risk, and sector limits inside the transaction. Do not trust client risk fields.

### P-002: Sector exposure uses invested market value, not configured capital

**Severity:** P1/P2 depending on intended policy
**Evidence:** `src/stock_screener/portfolio.py:1152-1167`; new positions start with `current_price=0`.

**Impact:** Cash and unrefreshed positions distort concentration. `max_sector_pct` is not enforced at admission.

**Fix:** Choose and document the denominator, use cost basis when quotes are unavailable, and reject policy violations.

### P-003: Trailing stop initialization and check order are wrong

**Severity:** P2
**Evidence:** `src/stock_screener/portfolio.py:714-726, 1128-1140`.

**Impact:** A new trailing position starts at the hard stop, and a newly raised trailing stop is checked only on the next poll.

**Fix:** Initialize from the peak and update before checking exits.

### P-004: Quote freshness is not represented

**Severity:** P2
**Evidence:** `src/stock_screener/portfolio.py:1024-1053`.

**Impact:** `previousClose` can be accepted as the current price, and failed refreshes retain stale values without returning per-ticker status.

**Fix:** Persist quote timestamp/status and suppress or label alerts based on stale data.

### P-005: Portfolio alert signatures are aggregate-level

**Severity:** P2
**Evidence:** `src/stock_screener/portfolio.py:841-951`.

**Impact:** Per-channel claims and retries are implemented, but adding a new event changes the aggregate signature and can resend old events. A cleared event can also be suppressed when it recurs with the same signature.

**Fix:** Persist event-level keys and transition state.

### P-006: R:R and take-profit direction are not validated

**Severity:** P2
**Evidence:** `src/stock_screener/price_target.py:60-63`; `backend/models.py:621-647`.

**Impact:** A long target below entry is reported as positive reward, and a below-entry take-profit can trigger immediately.

**Fix:** Make R:R direction-aware and reject invalid long target relationships.

## 8. Custom strategies, scale, and observability

### S-001: Custom strategy indicator periods are not wired

**Severity:** P2 / conditional
**Evidence:** `TechnicalEngine.enrich()` and `Backtester.run_multi()` use default indicator periods. A custom `VolumeBreakoutStrategy(vol_sma_period=50)` receives only `VOL_SMA_20`.

**Fix:** Add strategy indicator specifications or reject unsupported custom configurations.

### S-002: Some provider failures look like empty success

**Severity:** P2
**Evidence:** `screen_chain.py:186-188` and `multi_timeframe.py:184-185` catch failures and return no result; smart-screen and MTF responses do not expose structured errors.

**Fix:** Return per-ticker errors and distinguish “no matches” from “provider unavailable.”

### S-003: Scan responses are unbounded

**Severity:** P2
**Evidence:** signal and alert requests permit 100 tickers and up to 3,650 days; responses and React tables have no pagination or virtualization.

**Fix:** Add result limits, pagination, message caps, and table virtualization.

### S-004: Cache maintenance is synchronous and repeated

**Severity:** P2
**Evidence:** `data_loader.py:127-165` scans the entire cache after writes.

**Fix:** Run quota maintenance periodically or maintain incremental counters.

## 9. Security and deployment plan

### SEC-001: Long-lived bearer token in localStorage

**Severity:** P1 conditional
**Evidence:** `frontend/src/api.js:13-19`, `src/stock_screener/jwt_auth.py:38, 82-104`.

**Impact:** Any same-origin script execution or compromised browser extension can exfiltrate a 30-day token.

**Fix:** Prefer HttpOnly/Secure/SameSite cookies with CSRF protection, or short-lived in-memory access tokens with rotating refresh tokens.

### SEC-002: Local logout can be misleading

**Severity:** P2
**Evidence:** `frontend/src/main.jsx:141` clears the local token even when the server logout request fails.

**Fix:** Surface logout failure and retry server revocation, or use a server-side session design that cannot be silently skipped.

### SEC-003: Legacy portfolio permissions

**Severity:** P2 conditional
**Evidence:** New writes are hardened, but an existing ignored `data/portfolio.json` can remain mode `0644` until migrated or removed.

**Fix:** Chmod/migrate the legacy file during cutover and remove it after the rollback window.

### SEC-004: Proxy identity must be verified

**Severity:** P2 conditional
**Evidence:** `render.yaml:7` uses `--no-proxy-headers`; trusted proxy CIDRs are not fixed in the repository.

**Impact:** The safe default avoids spoofed forwarded headers, but a shared proxy address can collapse rate-limit buckets.

**Fix:** Verify the actual Render client address and configure the platform-supported mechanism.

### SEC-005: Reproducible Python dependencies

**Severity:** P1/P2
**Evidence:** Python dependencies use lower bounds only; no hash-locked requirements file exists. GitHub Actions use mutable tags.

**Fix:** Use a reviewed lock/hash strategy, pin Actions to commit SHAs, and add dependency scanning.

## 10. CI and test plan

The intended workflow is `.github/workflows/ci.yml`, but it must be tracked before it is a gate. It should run on the declared Python 3.12 environment and include:

- `pip install -e ".[dev]"` or an equivalent locked install
- `pytest -q`
- coverage with an agreed threshold
- `ruff check backend src tests`
- mypy, or an explicitly documented exception
- `npm ci --prefix frontend`
- `npm audit --prefix frontend --audit-level=high`
- `npm run build --prefix frontend`
- a clean-package smoke test
- at least one browser/E2E critical-flow test

Required regression tests include:

- entry-bar stop and target sequencing
- exact two-sided commission accounting
- drawdown percentage denominator
- one-share risk/capital rejection
- weekly holding and Sharpe frequency
- MTF look-ahead and unknown weekly state
- latest-signal position-plan selection
- yfinance inclusive/exclusive date boundary
- alert historical filtering, message cap, and per-channel retry
- earnings unknown and fail-closed behavior
- portfolio capital/risk/sector admission
- trailing-stop update ordering
- stale quote handling
- below-entry R:R and take-profit validation
- migration of existing alert capability
- scheduler restart, failure, and concurrent-run behavior

## 11. Phased remediation sequence

### Phase 0: Release provenance

- [ ] Track the complete migration and lockfile.
- [ ] Add `AUDIT_PLAN.md` to the commit.
- [ ] Add `httpx` to the test dependency set.
- [ ] Verify from a clean clone.
- [ ] Do not commit `.env`, `data/`, `node_modules/`, or `frontend/dist/`.

### Phase 1: Trading correctness

- [ ] Fix backtest entry-bar execution.
- [ ] Implement two-sided fees.
- [ ] Correct drawdown percentage.
- [ ] Enforce risk/capital ceilings.
- [ ] Fix weekly holding and Sharpe.
- [ ] Fix MTF as-of-state and metadata.
- [ ] Separate historical signals from actionable plans.

### Phase 2: Data and alert safety

- [ ] Fix yfinance end-date conversion.
- [ ] Make earnings status explicit and fail closed.
- [ ] Add signal event IDs and latest-bar filtering.
- [ ] Add message size limits and channel delivery receipts.
- [ ] Select one scheduler authority.
- [ ] Surface provider failures in smart-screen and MTF responses.

### Phase 3: Portfolio controls

- [ ] Enforce capital, risk, and sector admission.
- [ ] Define sector exposure denominator.
- [ ] Fix trailing initialization and update order.
- [ ] Persist quote freshness/status.
- [ ] Make portfolio alert state event-level.
- [ ] Validate direction of R:R and target levels.

### Phase 4: Production hardening

- [ ] Move away from long-lived localStorage bearer tokens.
- [ ] Make logout failure visible and recoverable.
- [ ] Verify proxy identity and Render headers.
- [ ] Migrate or remove the legacy portfolio file.
- [ ] Add dependency and secret scanning.
- [ ] Add browser/E2E coverage.

### Phase 5: Release validation

- [ ] Run the full suite on Python 3.12 with declared dependency floors.
- [ ] Run the frontend build and audit from a clean checkout.
- [ ] Run a clean Render-equivalent startup.
- [ ] Verify login, settings, per-user isolation, alert capability, and portfolio transactions.
- [ ] Verify live yfinance JST freshness.
- [ ] Verify Telegram/Slack delivery and retry behavior.
- [ ] Verify browser flows at mobile, tablet, and desktop widths.
- [ ] Review the final diff for secrets, generated files, and unrelated changes.

## 12. Definition of done

The project is not considered production-ready until:

- The current runtime is reproducible from a clean Git checkout.
- CI is tracked and green on the declared runtime.
- No P0/P1 audit finding remains unresolved or explicitly waived.
- Backtest outputs have documented execution, fee, holding-period, and drawdown semantics.
- Signal and alert output is current, bounded, and idempotent.
- Earnings unknown states cannot be displayed or treated as safe.
- Portfolio admission enforces capital, risk, sector, and target constraints.
- Production alert destinations and scheduler ownership are explicit.
- Security headers, session handling, proxy trust, and disk permissions are verified in the deployed environment.
- Unit, integration, API, and browser tests cover the critical flows.
- The release owner signs off on the remaining research-only limitations.

## 13. Current recommendation

Keep the application labeled **internal research prototype / risky alpha** until the Phase 0-3 items are complete. The existing test pass and frontend build are useful evidence of basic integration, but they do not override the unresolved trading, alert, earnings, portfolio, and release-provenance risks.
