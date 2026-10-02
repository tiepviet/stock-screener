# TSE Stock Screener Audit and Remediation Plan

**Audit date:** 2026-09-25
**Baseline:** `f36dd9a` (`main` / `origin/main`)
**Reviewed artifact:** current dirty working tree, including untracked migration files
**Overall status:** **BLOCKED for public production release**
**Production-readiness score:** **45/100** (up from 40/100 after the remediation commits on this branch)

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
| `/usr/bin/python3 -m pytest -q` | 248 passed, 1 warning |
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
- Persisted alert capability checks for delivery and auto-scan, re-checked immediately before every irreversible send.
- Single broadcast authority: the FastAPI lifespan scheduler, with the GitHub Actions scan workflow retired.
- Fail-closed earnings status (`known`/`unknown`/`error`) surfaced through the API and a distinct Unknown badge in the UI.
- Bounded alert messages, per-channel portfolio alert claims, and failed-channel retry state.
- Portfolio admission enforces per-position capital, aggregate capital, per-trade risk, and sector concentration inside the locked transaction.
- Canonicalized forwarded-address parsing so equivalent IPv6 spellings share one rate-limit bucket; alert scans use the alert-delivery budget, and immutable assets have their own bucket.
- Atomic watchlist batch writes, legacy-migration owner identity verification, and Telegram test-message HTML escaping.
- Persistent Render disk, health check, one-worker configuration, and pinned Node/Python versions.
- React build succeeds and the current npm dependency audit is clean.
- Streamlit has been moved to an explicitly unsupported `legacy/` reference directory.

## 4. Release blockers

### R-001: Runtime and CI artifacts are untracked

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P0 / release blocker
**Evidence:** At baseline (`f36dd9a`) `backend/`, `frontend/`, `legacy/`, `.github/workflows/ci.yml`, and multiple tests were untracked while `render.yaml` required them. All 45 migration paths are now tracked on this branch, and `git ls-tree HEAD` contains them.

**Acceptance criteria met:**

- All runtime, frontend, lockfile, CI, legacy-reference, and test files are tracked.
- No ignored secrets, databases, caches, `node_modules`, or build output are committed.

**Follow-up:** merge the branch; a Render deploy pointed at `main` still runs the old Streamlit app until this branch lands.

### R-002: Python test dependency is undeclared

**Status:** Resolved for CI in `audit/fastapi-react-migration`
**Severity:** P1
**Evidence:** API tests import `fastapi.testclient.TestClient`. `httpx>=0.27.0` is now in the `dev` extra of `pyproject.toml` and CI installs `pip install -e ".[dev]"`.

**Follow-up:** `README.md` still tells developers to run `pip install -r requirements.txt`, which omits `httpx`, so a clean local `pytest` still fails. Update the documented setup command and run the full suite from a clean Python 3.12 environment.

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

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P1 / High
**Evidence:** `format_summary_report()` had no cap or splitting; a synthetic 100-signal report was 5,777 characters, above Telegram's 4,096-character limit. `_bounded_alert_message()` in `src/stock_screener/alert.py` now caps output at 3,500 characters for both single-signal and summary reports, covered by `tests/test_alert.py`.

**Impact:** Large scans previously failed delivery; per-channel failure detail is still reduced to a boolean, and the raw-text cap can truncate inside an HTML tag in the Telegram payload.

**Fix:** Done for the size bound. Follow-up: truncate on tag boundaries and return per-channel failure details.

### D-004: Scheduler and GitHub workflow can both broadcast

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P1 / High
**Evidence:** `backend/scheduler.py` and `.github/workflows/daily-scan.yml` were independent delivery paths. The workflow is now a manual-only retirement switch and performs no delivery; `README.md` documents the FastAPI lifespan scheduler as the sole broadcaster.

**Impact:** Duplicate global broadcasts are no longer possible through two schedulers.

**Fix:** Done. Residual risk: the scheduler must stay single-instance.

### D-005: Earnings failures fail open

**Status:** Partially resolved in `audit/fastapi-react-migration`
**Severity:** P1 / High
**Evidence:** `EarningsInfo` now carries `status` (`known`/`unknown`/`error`) and `error`; `filter_safe()` and `backend/services.py:check_earnings()` classify any non-`known` or dateless result as risky, covered by `tests/test_earnings.py`.

**Impact:** A provider outage no longer appears to confirm that a trade is safe.

**Fix:** Done for classification and display. Still open: signal and alert admission paths do not invoke the earnings calendar at all.

### D-006: React displays unknown earnings as Clear

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P1 / High
**Evidence:** `frontend/src/main.jsx` previously ignored the API `unknown` list. The earnings view now renders a distinct muted `Unknown` badge for those tickers.

**Fix:** Done. Follow-up: add a browser-level regression test for the Unknown badge.

## 7. Portfolio and target findings

### P-001: Manual portfolio admission bypasses risk controls

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P1 / High
**Evidence:** `PortfolioTracker._enforce_admission_limits()` now runs inside the locked write transaction and rejects positions that exceed per-position capital, aggregate committed capital, per-trade risk (`risk_per_trade`, default 1%), or the sector concentration limit. `backend/services.py` wires the account's `risk_per_trade` into the tracker. Covered by `tests/test_hardening.py`.

**Impact:** The probe that previously stored a ¥100,000,000 position against ¥1,000 of capital now returns HTTP 422 and stores nothing.

**Fix:** Done. Follow-up: document the risk-per-trade source of truth in the UI sidebar contract.

### P-002: Sector exposure uses invested market value, not configured capital

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P1/P2 depending on intended policy
**Evidence:** Concentration is now measured on cost basis (`_sector_cost_basis()`) as a share of configured capital and enforced at admission, so unrefreshed positions with `current_price == 0` can no longer report zero exposure and silently bypass the cap.

**Fix:** Done.

### P-003: Trailing stop initialization and check order are wrong

**Status:** Partially resolved in `audit/fastapi-react-migration`
**Severity:** P2
**Evidence:** `add_position()` now seeds `trailing_stop` from the entry price and `trail_pct` (never from the hard stop).

**Impact:** Seeding is fixed. The check order still needs `update_trailing_stops()` to run before exit evaluation so a newly raised stop is evaluated in the same pass.

**Fix:** Move the trailing update ahead of the stop/target checks.

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

### P-007: Failed take-profit delivery is permanently lost

**Severity:** P2 with direct financial consequence
**Evidence:** `src/stock_screener/portfolio.py` `full_check()` marks `tp_hit=True` and commits before external delivery. `backend/main.py` then records `delivered=False` for a failed channel, but the next check no longer returns the take-profit event, so it can never be retried. Probe: `take_profits={'7203': [0]}` followed by an empty event set after a simulated failed send.

**Impact:** A take-profit notification that fails to deliver is silently dropped rather than retried, so the operator can miss the exit signal.

**Fix:** Separate detection from acknowledgment and retain pending event state until delivery succeeds or the event explicitly expires.

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

**Status:** Resolved in `audit/fastapi-react-migration`
**Severity:** P2 conditional
**Evidence:** New writes are hardened, and `migrate_legacy_portfolio_to_user()` now chmods the legacy `data/portfolio.json` to `0600` before reading it, with a migration marker to prevent repeat copies.

**Fix:** Done for the migration path. Follow-up: remove the legacy file after the rollback window.

### SEC-004: Proxy identity must be verified

**Severity:** P1 conditional, elevated by SEC-006
**Evidence:** `render.yaml:7` uses `--no-proxy-headers`; trusted proxy CIDRs are not fixed in the repository. `backend/security.py` ignores forwarded headers unless the direct peer matches a configured network, and rate-limit keys are policy plus resolved client IP.

**Impact:** The safe default avoids spoofed forwarded headers, but a shared proxy address can collapse rate-limit buckets for every user and turn per-IP login throttling into a global throttle.

**Fix:** Verify the actual Render peer address and configure the platform-supported mechanism. Do not relax `--no-proxy-headers` before that verification.

### SEC-005: Reproducible Python dependencies and build-time secret exposure

**Severity:** P1
**Evidence:** Python dependencies use lower bounds only; no hash-locked requirements file exists and GitHub Actions use mutable tags. `render.yaml` runs `npm ci`, `npm run build`, and `pip install -r requirements.txt` while `TSE_ADMIN_PASSWORD` and a generated `TSE_JWT_SECRET` are service environment variables, which Render also exposes to build steps.

**Impact:** A compromised dependency, install script, or build step can read production credentials, and unreviewed version drift can change a build without a code change.

**Fix:** Use a reviewed lock/hash strategy, pin Actions to commit SHAs, keep runtime secrets out of build steps, and add dependency/secret scanning.

### SEC-006: Account-wide login lockout enables unauthenticated denial of service

**Severity:** P1 / High, confirmed
**Evidence:** `src/stock_screener/auth.py` defines `MAX_ACCOUNT_FAILED_ATTEMPTS = 20` over a 15-minute window and counts failures per username independent of source IP. `backend/main.py` returns HTTP 429 for a locked account.

**Impact:** An unauthenticated attacker can submit 20 wrong passwords for the documented `admin` username from any number of IPs and then block legitimate logins for 15 minutes. This is an availability failure, not credential compromise, and it becomes global when traffic arrives through one proxied address.

**Fix:** Replace hard account lockout driven by untrusted failures with progressive per-source throttling plus risk scoring or CAPTCHA, add an administrative recovery path, and cap global login concurrency so bcrypt work cannot saturate the worker pool.

### SEC-007: Authenticated scans can exhaust the shared worker pool

**Severity:** P1 conditional
**Evidence:** `backend/models.py` accepts up to 100 tickers with 3,650-day lookbacks, and `backend/services.py` builds up to eight worker threads per scan request. `backend/security.py` rate limits by request count only, with no global or per-user concurrency ceiling.

**Impact:** Any authenticated account can burst expensive scans. Provider semaphores cap active provider calls, but queued requests still hold synchronous handler threads and can starve login, portfolio, and health endpoints.

**Fix:** Add global and per-user request semaphores acquired before thread-pool creation, reject or queue excess work, and lower interactive ticker/history limits.

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

- [x] Track the complete migration and lockfile.
- [x] Add `AUDIT_PLAN.md` to the commit.
- [x] Add `httpx` to the test dependency set.
- [ ] Verify from a clean clone.
- [x] Do not commit `.env`, `data/`, `node_modules/`, or `frontend/dist/`.

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
- [x] Make earnings status explicit and fail closed (classification, API, UI; signal admission still open).
- [ ] Add signal event IDs and latest-bar filtering.
- [x] Add message size limits.
- [ ] Add channel delivery receipts and retry state for take-profit events (P-007).
- [x] Select one scheduler authority.
- [ ] Surface provider failures in smart-screen and MTF responses.

### Phase 3: Portfolio controls

- [ ] Enforce capital, risk, and sector admission (in place; wire the sidebar risk percent through to the tracker).
- [x] Define sector exposure denominator (cost basis vs configured capital).
- [ ] Fix trailing initialization and update order (initialization done; ordering still open).
- [ ] Persist quote freshness/status.
- [ ] Make portfolio alert state event-level.
- [ ] Validate direction of R:R and target levels.

### Phase 4: Production hardening

- [ ] Move away from long-lived localStorage bearer tokens.
- [ ] Make logout failure visible and recoverable.
- [ ] Verify proxy identity and Render headers (SEC-004).
- [ ] Replace the account-wide login lockout with progressive throttling and admin recovery (SEC-006).
- [ ] Add global and per-user request concurrency limits (SEC-007).
- [ ] Lock/hash Python dependencies and pin Actions to commit SHAs (SEC-005).
- [x] Migrate or remove the legacy portfolio file.
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

Keep the application labeled **internal research prototype / risky alpha** until Phase 1-3 are complete. Release provenance is now resolved on this branch, and 234 tests plus a clean frontend build and audit are useful evidence of basic integration. They do not override the unresolved trading correctness (B-001 to B-007), alert idempotency (D-002), portfolio admission and target validation (P-001, P-002, P-006, P-007), data boundary (D-001), and security findings SEC-001, SEC-004, SEC-005, SEC-006, and SEC-007.

Do not merge to `main` and point Render at it until at minimum the eight P1 items in section 14 are closed or explicitly waived by the release owner.

## 14. Ordered blocker list

1. Commit the remaining working-tree changes so `HEAD` equals the verified tree. (Done for the earnings and admission hardening; re-verify any further concurrent work before shipping.)
2. Fix backtest fee accounting (B-002), drawdown denominator (B-003), and entry-bar exits (B-001) with exact regression tests.
3. Enforce risk and capital ceilings in `RiskManager` sizing itself (B-004); admission-side enforcement is already in place.
4. Make MTF weekly state as-of-signal and tri-state; separate historical signal logs from actionable plans (B-006, B-007).
5. Add durable signal event IDs to `AlertScanner` and include signal dates in reports (D-002).
6. Replace the account-wide login lockout (SEC-006) and add scan concurrency limits (SEC-007).
7. Convert the public inclusive end date to yfinance's exclusive boundary (D-001).
8. Reconcile the Render proxy configuration with the trusted-proxy policy (SEC-004).
