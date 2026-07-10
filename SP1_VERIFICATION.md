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
- [ ] Confirm all checks pass in the packaged executable.

## D. Recovery and reconnect

- [x] Stable Recovery contract exists.
- [x] Stable Smart Reconnect contract exists.
- [x] Structured operation result exists.
- [ ] Implement a concrete game/window adapter.
- [ ] Detect missing, disconnected, unreadable, or overlapped game window.
- [ ] Prevent input when the target window cannot be verified safely.
- [ ] Implement and test a real reconnect sequence.
- [ ] Verify the player remains in control outside explicitly automated modes.

## E. Tests

- [x] Bootstrap persistence test.
- [x] Service registry test.
- [x] Boundary contract tests.
- [x] Self-check tests.
- [x] User-facing status formatting tests.
- [ ] Run all tests successfully on Windows Python 3.12.
- [ ] Add concrete adapter and recovery integration tests.

## F. Build and delivery

- [x] PyInstaller specification exists.
- [x] Windows workflow installs dependencies and runs tests.
- [x] Workflow verifies `dist/FLASH.exe` exists.
- [x] Workflow uploads the executable as an artifact.
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
