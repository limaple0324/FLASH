# SP1 Verification Checklist

This document is the delivery gate for FLASH SP1. A feature is not considered complete merely because code or a workflow file exists.

## A. Repository and synchronization

- [x] GitHub default branch is `main`.
- [x] Desktop synchronization script tracks `origin/main`.
- [x] Synchronization refuses to overwrite uncommitted local changes.
- [x] Scheduled task starts shortly after registration and repeats every 15 minutes.
- [ ] Verify the scheduled task exists on the target Windows computer.
- [ ] Verify `Desktop\FLASH` HEAD matches `origin/main`.

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
- [x] Desktop window displays detailed results.
- [x] Corrupt configuration is preserved and rebuilt automatically.
- [ ] Confirm all checks pass in the packaged executable on the target desktop.

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
- [x] Background capture result is shown in the SP1 window and JSON report.
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
- [ ] Confirm the current Windows workflow run passes.
- [ ] Add target-desktop integration tests for capture, overlap, and reconnect behavior.

## F. Build and delivery

- [x] PyInstaller specification exists.
- [x] Windows workflow installs dependencies and runs tests.
- [x] Workflow verifies `dist/FLASH.exe` exists.
- [x] Workflow runs packaged `--self-check` mode.
- [x] Workflow verifies persistent config, log, and JSON report creation.
- [x] Workflow records version, commit, build time, and SHA-256.
- [x] Workflow uploads a complete verification bundle.
- [ ] Confirm a successful GitHub Actions run for current `main`.
- [ ] Download and launch the current artifact.
- [ ] Confirm config and log persistence after closing and reopening.
- [ ] Confirm no console window appears.
- [ ] Complete user acceptance test on the target desktop.

## Delivery states

- **Engineering foundation:** core boundaries and diagnostics exist.
- **Engineering verification build:** executable starts and self-check passes, but real game adapters may still be absent.
- **Player test build:** concrete target-window, recovery, and reconnect behavior is integrated and tested.
- **SP1 complete:** all required checklist items are checked and the target Windows desktop acceptance test passes.
