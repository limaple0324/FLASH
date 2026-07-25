# SP1 Verification Checklist

This document is the delivery gate for FLASH SP1. A feature is not considered complete merely because code or a workflow file exists.

Scope note (2026-07-25): new work is restricted to SP1 on
`sp1/completion-2026-07-25`, based on `main@538bdbc`. The latest verified source
commit is
`03db0624fb64e7b5997502558914a0f706da7b79`. The `5db5b5f` local baseline
passed 171 tests, Python compilation, 8/8 source self-checks, and
`git diff --check`; the subsequent hash-compatibility fix passed the 24 affected
updater/release-verifier tests and one focused regression test.

GitHub Actions run #118 passed compilation, the complete test suite, source and
packaged self-checks, Windows EXE creation, bundle layout, metadata, hashes, and
artifact upload. The `689a186` snapshot also passed its bundled verifier under
local Windows PowerShell 5.1. Its ZIP SHA-256 matches the GitHub artifact digest:
`30aca05eb2e8c84f10c58a86cc34af89cda8559b31989b8b6cb266e007b231fa`.
On Windows 11 build 26200, the downloaded artifact also passed two GUI launches,
normal close/reopen, isolated config/log/registry persistence, 8/8 packaged
self-check results, and no-console verification. This used the current Windows
account with isolated `APPDATA`/`LOCALAPPDATA`, not a clean new user account, and
did not exercise a real game flow. SP1 is therefore still not formally complete.

GitHub Actions run #120 for `f419e07` passed the complete workflow and published
the SP1-only release to `release/sp1`. The permanent ZIP SHA-256 matches artifact
`8616031855`:
`746fc2c630b77e7d7e3d2db87089716cfe5ebf0fda37e5cf9010d264d5fd6b66`.
On the target Windows 11 computer, its real `更新輔.cmd` successfully resolved
`release/sp1`, downloaded one fixed release commit, passed pre-install
verification, applied the transaction, and passed post-install verification.

GitHub Actions run #123 for `cee31ac` passed all 183 tests and published the
complete installer/updater bundle. Its permanent ZIP SHA-256 matches artifact
`8616207872`:
`40af7b7c6a8e8ab8c768cdf6aa5961cb82f1a28f6c7075204b316adbec52053a`.
The real installer then passed source, staged, and installed bundle verification
on Windows 11 and created exactly one isolated `輔.lnk` with the expected target,
working directory, and executable icon.

GitHub Actions run #124 for `03db062` passed 184 tests, source and packaged
8/8 self-checks, Windows EXE creation, bundle verification, artifact upload,
and SP1-only publication. The permanent ZIP SHA-256 matches artifact
`8616418914`:
`e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`.
On Windows 11 build 26200, the same-source packaged window displayed the full
self-check details, conservative target-window state, all three background
capability states, registry state, disabled-input notice, and isolated log path.

Every new item must be confirmed as discussed before implementation. The
player-viewable window-registry area and the five frozen resource areas remain
paused. SP2 and SP3 work does not resume until the prior delivery stage is complete.

## A. Repository and delivery update

- [x] GitHub default branch is `main`.
- [x] SP1 completion uses a dedicated branch based on `main`.
- [x] SP1 has an independent delivery area and status file.
- [x] The player-facing update entry is the single `更新輔.cmd`.
- [x] Restrict `release/latest` publication to an approved `main` push.
- [x] Give the independent SP1 build an update source that cannot advance to
  SP2, SP3, or the complete integrated delivery.
- [x] Confirm the obsolete scheduled Git sync task is absent or disabled.
- [x] Implement same-commit, full-payload verification and transactional update
  with rollback and failure-injection coverage.
- [x] Run `更新輔.cmd` on the target computer and verify the installed build.
- [x] Implement and verify a complete first installer with rollback and one
  executable-backed shortcut.

## B. Core startup

- [x] Persistent config and log paths are created.
- [x] Core services are registered through `AppContext`.
- [x] Bootstrap returns structured startup status.
- [x] Startup exceptions are logged and shown to the user.
- [x] Packaged executable supports non-GUI `--self-check` verification.
- [x] CI verifies packaged config, log, and self-check report creation.
- [ ] Run the packaged executable on a clean Windows user account.

## C. SP1 self-check

- [x] Paths check.
- [x] Configuration check.
- [x] Logger write check.
- [x] Event bus delivery check.
- [x] Recovery boundary check.
- [x] Smart Reconnect boundary check.
- [x] External adapter boundary check.
- [x] Detailed self-check result formatting is covered by source tests.
- [x] Corrupt configuration is preserved and rebuilt automatically.
- [x] Confirm detailed results display correctly in the packaged application on
  the target Windows 11 desktop.
- [x] Confirm all checks pass in the packaged executable on the target desktop.

## D. Recovery, reconnect, and window safety

- [x] Stable Recovery contract exists.
- [x] Stable Smart Reconnect contract exists.
- [x] Structured operation result exists.
- [x] Read-only Windows top-level window enumeration adapter exists.
- [x] Adapter rejects missing, ambiguous, minimized, or invalid-bounds targets.
- [x] Adapter supports operation-area-specific overlap checks.
- [x] Adapter never sends input and clears cached targets on shutdown.
- [x] Background capability report model exists.
- [x] Read-only Windows `PrintWindow` capture probe exists.
- [x] Blank or failed background captures are rejected conservatively.
- [x] Background capture result model and JSON report output are covered by
  source tests.
- [x] Confirm the result is shown correctly in the packaged SP1 window on the
  target Windows 11 desktop.
- [ ] Configure the real Adobe Flash Player 11 window identity.
- [ ] Verify background capture while the game is partially covered.
- [ ] Verify background capture while the game is not foreground.
- [ ] Verify background capture while the game is minimized.
- [ ] Add a user-approved harmless background-input probe.
- [ ] Verify non-foreground background input on the target desktop.
- [ ] Verify minimized background input on the target desktop.
- [ ] Implement and test a real reconnect sequence.
- [ ] Verify the player remains in control outside explicitly automated modes.

## E. Tests

- [x] Bootstrap persistence test.
- [x] Service registry test.
- [x] Boundary contract tests.
- [x] Self-check tests.
- [x] User-facing status formatting tests.
- [x] Configuration recovery and atomic-write tests.
- [x] Read-only Windows target-window adapter tests.
- [x] Background capability framework tests.
- [x] Windows background capture probe tests.
- [x] Windows workflow is configured to run all tests on Python 3.12.
- [x] Local baseline passes 171 tests at `5db5b5f`; the subsequent
  hash-compatibility change passes all 24 affected tests and its regression test.
- [x] Win32 adapter and transactional updater received independent focused
  reviews with no remaining blocker.
- [x] GitHub Actions run #118 for `689a186` passes the complete workflow.
- [x] GitHub Actions run #120 for `f419e07` passes the complete workflow and
  publishes only to `release/sp1`.
- [x] GitHub Actions run #123 for `cee31ac` passes all 183 tests and publishes
  the complete installer/updater bundle only to `release/sp1`.
- [x] GitHub Actions run #124 for `03db062` passes all 184 tests, publishes only
  to `release/sp1`, and its packaged status window passes Windows 11 display
  acceptance.
- [ ] Add target-desktop integration tests for capture, overlap, and reconnect behavior.

## F. Build and delivery

- [x] PyInstaller specification exists.
- [x] Windows workflow installs dependencies and runs tests.
- [x] Workflow verifies `dist/FLASH.exe` exists.
- [x] Workflow runs packaged `--self-check` mode.
- [x] Workflow verifies persistent config, log, and JSON report creation.
- [x] Workflow records version, commit, build time, and SHA-256.
- [x] Workflow uploads a complete verification bundle.
- [x] Older `3c92253` engineering snapshot passed its bundled release verifier
  and packaged `--self-check`.
- [x] Verify the `689a186` bundle, metadata, manifest, hashes, artifact digest,
  bundled Windows PowerShell 5.1 verifier, and cloud packaged `--self-check`.
- [x] Verify and permanently save the `f419e07` SP1-only release, its artifact
  digest, `release/sp1` identity, and real target-computer updater flow.
- [x] Verify and permanently save the `cee31ac` complete installer/updater
  release and its isolated Windows 11 first-install/shortcut flow.
- [ ] Confirm a successful GitHub Actions run for current `main`.
- [x] Download and launch the current artifact on Windows 11 build 26200 with an
  isolated data profile.
- [x] Confirm config, log, self-check, and window-registry persistence after
  closing and reopening.
- [x] Confirm no console window appears.
- [ ] Complete user acceptance test on the target desktop.

## Delivery states

- **Engineering foundation:** core boundaries and diagnostics exist.
- **Engineering verification build:** executable starts and self-check passes, but real game adapters may still be absent.
- **Player test build:** concrete target-window, recovery, and reconnect behavior is integrated and tested.
- **SP1 complete:** all required checklist items are checked and the target Windows desktop acceptance test passes.

## Independent delivery records

- SP1 delivery area: `deliverables/sp1/`
- SP1 status file: `docs/01_輔_SP1_基礎系統_狀態與驗收.md`
- Overall project file: `docs/00_輔_專案完整總整理與未來路線圖.md`
- Latest verified engineering snapshot:
  `FLASH-SP1-Windows-0.1.2-689a186-snapshot.zip`, source
  `689a186e5cd3e93e72e98c25e2840deb49e0a637`, GitHub Actions run
  `30144187526`, artifact `8615455764`, SHA-256
  `30aca05eb2e8c84f10c58a86cc34af89cda8559b31989b8b6cb266e007b231fa`.
- Permanent evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-689a186-snapshot.md`.
- Latest SP1-only release:
  `FLASH-SP1-Windows-0.1.2-f419e07-sp1-release.zip`, source
  `f419e07ebdef5e4836e6229df082c6222b2ef551`, GitHub Actions run
  `30146010560`, artifact `8616031855`, publication commit
  `56c815680c9f8cc281f1674f757a3c11f79beba0`, SHA-256
  `746fc2c630b77e7d7e3d2db87089716cfe5ebf0fda37e5cf9010d264d5fd6b66`.
- Latest release evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-f419e07-sp1-release.md`.
- Latest complete installer/updater release:
  `FLASH-SP1-Windows-0.1.2-cee31ac-sp1-release.zip`, source
  `cee31ac2124ba3584f3d278d5dd67330d8cb312a`, GitHub Actions run
  `30146543198`, artifact `8616207872`, publication commit
  `65e58a70543d082cbe96c207e4cda5bf5ca22360`, SHA-256
  `40af7b7c6a8e8ab8c768cdf6aa5961cb82f1a28f6c7075204b316adbec52053a`.
- Latest complete release evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-cee31ac-sp1-release.md`.
- Latest complete safe-status release:
  `FLASH-SP1-Windows-0.1.2-03db062-sp1-release.zip`, source
  `03db0624fb64e7b5997502558914a0f706da7b79`, GitHub Actions run
  `30147184671`, artifact `8616418914`, publication commit
  `8c0c61bae3a1c3f97b8794162c7ba88a415a2e94`, SHA-256
  `e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`.
- Latest safe-status release evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-03db062-sp1-release.md`.
- The latest SP1 artifact record must include branch, full commit, version,
  tests, self-checks, SHA-256, build source, Windows evidence, and publication
  state.
