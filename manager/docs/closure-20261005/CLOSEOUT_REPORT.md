# WorkBuddy Manager maturity closeout

**Date:** 2026-10-05 (Asia/Shanghai)  
**Scope:** Existing WorkBuddy Manager and WorkBuddy-API gateway on the `xiaochen` server.  
**Production model calls during this validation:** 0.

## Current conclusion

The deployed source preserves the 130-file sticky-conversation, account-selection, priority, retry, and prompt-compatibility contract. A clean Git archive passed **121 modules / 2,125 tests / 0 failures / 0 errors / 4 skips** with the network namespace isolated and no production model calls. Synthetic role/recovery flows and the full Go suite also passed. GitHub Actions run #5 passed the Manager, frontend, and gateway jobs. These results do not establish that all 22 maturity checks are closed.

Production release `v1.0.79+closure.20261005.3` is active. Its signed manifest verified against the server trust anchor, all 491 managed source entries passed preflight, the web service is active, and `/api/readyz`, `/admin/login/`, `/dashboard`, and `/settings/upstream/` returned 200 after activation. The login stability revision 7 is included and deployed. Read-only database reconciliation confirms the `cn` trend covers all 14 dates from 2026-09-22 through 2026-10-05 with 90,162 request rows, including 4,283 failed rows; no trend dates are missing from storage. The release does not close every original maturity item; remaining gaps are recorded below.

## Verified candidate evidence

- The clean Git archive passed 121 isolated Linux modules / 2,125 tests / 0 failures / 0 errors / 4 skips; the external-network route was absent. GitHub Actions run #5 also passed all three jobs. No production model was called.
- Gateway Go packages and the 20,000-entry sticky-session mirror stress test passed. The stress test performed 100 atomic file writes in about 2.0 seconds (about 20 ms average); this is a fixture result, not a service-level promise.
- Real loopback TCP pooling handled 801 requests. The run reused connections and its Linux file-descriptor samples returned to the starting level after clients closed.
- The app API flow tested anonymous, viewer, and admin roles; upstream settings were saved, rejected on an unknown field without changing disk, and read back. The same flow forced readiness to fail and recover.
- A real SQLite `SQLITE_FULL` fault rolled back the synthetic usage event. A duplicate local alert was suppressed and a recovery transition was recorded.
- Two fixed-parameter static exports had identical hashes for all 235 generated files.
- A pinned Trivy 0.75.0 scan of the final gateway image found **0 critical / 0 high / 0 medium / 0 low** vulnerabilities; its CycloneDX SBOM contains 79 components. The final scan JSON and SBOM are included beside this report.
- The candidate migration was run against a private online-backup copy of production SQLite: `quick_check=ok`, 19 original tables and 161,448 rows preserved, with 27,895,040,000 legacy credit millionths projected. The live database was opened read-only.
- A read-only historical reconciliation found the 990-request difference arose from daily request counters counting all log rows while later reports counted requests with measured usage. Across Sep 14–26, token and credit differences were zero; 126,883 older log rows have no request ID. No historical rows or balances were rewritten.
- The gateway’s 130 protected files still match the live baseline. The final gateway image is `workbuddy2api-upgrade:20261005.3`, image ID `sha256:948ed2ae7c4e6d674a24805773a6d82b926ea0a5d7a2aa574620115305106312`, running as `app`.
- **Dashboard 14-day trend capacity fix:** `/opt/workbuddy-manager/server/bodyguard.py` previously held one of four body-buffer permits for the entire handler. Slow statistics queries could starve concurrent dashboard calls and yield `管理请求容量已满，请稍后重试`, even though the historical rows were present. Release `.3` releases the permit after body buffering/delivery (and immediately for bodyless GET handlers); the slow-handler regression passes. The 14-day database contains all dates and 90,162 request rows. A visual authenticated browser check remains unverified because browser automation returned EOF; no screenshot password was used.
- **Login stability revision 7 (see `login-stability-evidence.json`):** the deployed build removes the 350 ms vertical entry motion and keeps authentication loading until the server `/me` check completes. TypeScript, ESLint, 11 existing frontend test files, `npm audit` (0 findings), and two identical fixed-parameter static exports (235 files) pass. Production routes return 200; a visual browser session and authenticated end-to-end flow were not verified because the available browser automation backends returned EOF.

## Acceptance matrix

`CLOSED` means the stated acceptance boundary has evidence in this release. `PARTIAL` means code or isolated evidence exists but a required production, historical, business, multi-host, or third-party check remains. `BLOCKED` is reserved for a host-policy limit that this SSH MCP cannot change.

Current strict count: **6 closed, 16 partial, 0 policy blocked**. The production deployment is complete, but 16 acceptance items still need production, multi-host, business-owner, or operational evidence; this is not “all 22 closed.”

| ID | Status | Evidence and remaining closure condition |
|---|---|---|
| F01 | PARTIAL | Release v1.0.79+closure.20261005.3 is signed and active; readiness and production routes pass. Full cutover fault-injection remains. |
| F02 | CLOSED (known scope) | Historical key findings from the prior audit remain corrected; this release publishes no credentials. Previously unreachable Git history is outside the scan’s proof. |
| F03 | PARTIAL | The Manager Web process still runs as root. Direct SSH was available for the authorized application deployment, but a dedicated service account and systemd sandbox have not been staged and proven against the live writable-path contract. No account or unit change was made in this release. |
| F04 | PARTIAL | Existing encrypted offsite recovery remains verified and the new signed release is included in the recovery contract. A new-host restore/RTO drill and independently escrowed second signing/decryption key remain. The configured backup job is daily, not hourly. |
| F05 | PARTIAL | Commits `44f34a4` (capacity fix) and `3fd64ec` (clean-checkout CI fixes) are on `main`. [GitHub Actions run #5](https://github.com/daniei-chen/WorkBuddy-API/actions/runs/37282141387) passed Manager, frontend, and gateway jobs. Branch-protection settings and signed build provenance remain unverified. |
| F06 | CLOSED | Stream terminal outcomes remain distinct from HTTP status; synthetic three-protocol tests pass. |
| F07 | CLOSED | Incremental UTF-8/SSE parsing, bounded buffers, tool output and stream endings pass the isolated regressions. |
| F08 | CLOSED | Invalid surrogate input rejects cleanly; legal Unicode is preserved in isolated tests. |
| F09 | PARTIAL | Durable owner/fencing, unknown-spend freeze, exact integer balances and manual adjustments are covered. Actual provider pricing, hard billing caps and multi-host ownership recovery are not available or proven. |
| F10 | PARTIAL | Gateway attempts now carry exact request IDs and structured account/model/realm/config-hash events; new rows identify exact versus timestamp-inferred account attribution. Production traffic after deployment must confirm collection and historical rows remain heuristic. |
| F11 | PARTIAL | Integer millionth projections, append-only idempotent financial events and SQLite-full rollback pass. Historical request-count semantics are explained, but events cannot be reconstructed for old rows without IDs; a replayable canonical history remains incomplete. |
| F12 | PARTIAL | Admin changes, failures and denials enter an append-only hash chain. Full background-job and every-route outcome coverage plus separately protected long-term audit storage remain. |
| F13 | CLOSED (schema boundary) | Typed, bounded writable schema rejects unknown fields and preserves the custom pool/sticky settings; source hash and effective configuration provenance are recorded. |
| F14 | PARTIAL | API body bounds/timeouts and container CPU/memory/PID limits are tested. The deployed admission fix releases body-buffer capacity once the body is handed to the app (or immediately for bodyless GETs); a slow-handler regression passes. A 12-request production smoke returned 12 expected authentication 401s and no capacity 503s. The Manager host process still lacks a memory cap, and authenticated peak production load is not measured. |
| F15 | PARTIAL | Python wheel hashes, frontend lock, Go sums, deterministic static output, the disclosed stack-guarded `braces` fork, and the pinned Trivy/79-component SBOM pass; the runtime image has 0 high/critical findings. GitHub Actions run #5 passed all jobs after tracking required Manager docs and correcting Trivy image-file permissions. Signed build provenance remains. The advisory identifies no officially patched `braces` release, so the compatible local patch remains disclosed and tested ([GitHub advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)). |
| F16 | PARTIAL | Existing selection algorithms remain byte-for-byte protected; process-death budget fencing and 20k sticky-mirror load tests pass. Multi-instance Redis migration and a live post-upgrade cold/warm session measurement remain. |
| F17 | PARTIAL | Existing prompt/transformation algorithms remain. Transformation summaries are recorded, but a complete passthrough semantic golden suite remains. |
| F18 | CLOSED (pool contract) | Actual TCP keep-alive reuse, bounded concurrency/cancellation, file-descriptor recovery and response closure passed in isolated Linux fixtures. Production tail latency is not claimed. |
| F19 | PARTIAL | Login stability revision 7 and the request-capacity fix are deployed in v1.0.79+closure.20261005.3; readyz and `/admin/login/`, `/dashboard`, and `/settings/upstream/` returned 200. Static build evidence confirms the entry motion is absent. Visual/authenticated chart verification and duplicate Nginx rule cleanup remain. |
| F20 | PARTIAL | Database write readiness, local alert dedup/recovery and health endpoints are tested. No external notification target is configured, and a 30-day SLO cannot be inferred from this deployment run. |
| F21 | PARTIAL | Prior audit found all 37 auth files had correct ownership and mode. A new normal refresh cycle must complete after release; no account was forcibly refreshed. |
| F22 | PARTIAL | Role permissions and configuration rejection are exercised with synthetic accounts. Pricing source, history retention periods, and deletion/reconciliation business contracts still need an owner’s decision. |

## Remaining operator work

1. **F03, privilege boundary:** stage a dedicated Web UID on a cloned service with an offline copy of the SQLite database; inventory every runtime write path; verify restart, upload, scheduled jobs, backup, readiness and rollback; then apply a systemd drop-in with explicit writable paths, `NoNewPrivileges`, filesystem protection, resource limits and a reboot test. The live Manager still runs as root.
2. **F05/F15, repository gates:** GitHub Actions run #5 passed all jobs. Configure and verify branch protection and signed build provenance separately.
3. **F19, login acceptance:** run an authenticated visual browser pass after browser automation is available; test fresh login, stale cached session, logout, refresh and direct `/settings/upstream/` navigation. Production route checks alone do not prove these flows.
4. **F04/F20, recovery and alerting:** restore onto a clean host, measure RTO from encrypted backup with the offline key, escrow a second signing/decryption key, and deliver an external alert to a configured destination.
5. **F09/F11/F22, business controls:** assign owners for provider pricing, hard-spend caps, event-retention duration, deletion and historical reconciliation. Keep the 990-row counter discrepancy documented; do not fabricate missing request IDs or ledger events.
6. **F16/F21, runtime operations:** complete a normal post-release account refresh and a real cold/warm sticky-session comparison across multiple gateway instances. Protected selection algorithms remain unchanged and all 130 baseline file hashes match.

## Recovery and integrity

The signed manifest enumerates release files and modes. Recovery archives contain the detached signature and trust anchor separately, verify that the archived anchor matches the installed custodian anchor, then validate the signature. Rollback retains the production database, token state and new consumption. Older unsigned code can be preserved as a byte-verified baseline; it is not retroactively signed.

## Deployment and repository closeout (2026-10-05)

- Signed release `v1.0.79+closure.20261005.3` passed installed-trust-anchor verification and server source preflight; all 491 managed release entries passed preflight. All 130 protected gateway algorithm files matched their contract and production hashes.
- Full isolated backend regression passed from a clean Git archive: 121 modules, 2,125 tests, 0 failures, 0 errors, 4 skips, and 0 production model calls. GitHub Actions run #5 passed Manager, frontend (TypeScript, ESLint, tests, build, audit), and gateway (Go tests, runtime build, Trivy) jobs.
- Release `v1.0.79+closure.20261005.3` has a signed manifest that verifies against the installed public trust anchor. The matching private key stayed on the workstation.
- Production checks passed after activation: `/api/readyz`, `/admin/login/`, `/dashboard`, and `/settings/upstream/` returned 200; the Manager service is active; gateway image `workbuddy2api-upgrade:20261005.3` is running at the manifest’s pinned image ID. The protected 14-day stats API returned 401 without a session as expected; a 12-request parallel smoke produced no capacity 503s.
- The first activation attempt exposed a deployment-controller bug: it addressed the Docker container name instead of the Compose service name. The first rollback command used the same wrong service name. The verified baseline pointer was restored and checked; the controller was corrected, the candidate Compose configuration was validated, and the signed activation then succeeded.
- The login entrance motion was removed and cached identity no longer ends the loading state before server validation. Static build and route delivery are verified; browser visual and authenticated workflow checks remain unverified because both UI automation backends returned EOF.
- Commits `44f34a4c60e992667e5bd8015875c989374e8434` and `3fd64ecc3ed017d84277d528432b7bbe84806cb5` were pushed fast-forward to GitHub `main` using the existing SSH deploy key, without force push or the pasted PAT. Run #4 exposed ignored `manager/CHANGELOG.md` and `manager/README.en.md` files required by tests, plus a Trivy container read-permission issue on `image.tar`. The follow-up tracks both docs and makes the image readable while retaining the read-only container mount. A fresh archive reproduced 2,125 tests with no failures/errors, and [Actions run #5](https://github.com/daniei-chen/WorkBuddy-API/actions/runs/37282141387) passed all jobs.
- A separate DSH proxy-session cookie was refreshed using the server's existing `dsh-pair-sync.service`; its active Nginx config passed `nginx -t`. This does not change WorkBuddy Manager credentials. The issuer-side revocation of the replaced JWT was not independently verified.
