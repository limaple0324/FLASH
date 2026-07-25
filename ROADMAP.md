# FLASH Roadmap

## SP1 — Core Foundation

Current target: complete the remaining SP1 Windows and real-game acceptance
gates without starting SP2 early.

- [x] Application entrypoint
- [x] Config manager
- [x] Path manager
- [x] App context
- [x] Event bus
- [x] Logger service
- [x] Test boundary
- [x] Plugin-ready package boundary
- [x] Workspace package boundary
- [x] Recovery boundary contract
- [x] Smart Reconnect boundary contract
- [x] API boundary specification in code
- [x] CI workflow definition
- [x] Obsolete scheduled main-branch sync removed; read-only status checker retained
- [x] Dedicated `release/sp1` channel that cannot advance to SP2/SP3
- [x] Transactional updater with full-payload verification and rollback
- [x] Complete first installer with rollback and one executable-backed shortcut
- [x] Full read-only status shown in the packaged player window

Remaining SP1 verification work:

- [x] GitHub Actions Windows run #127 succeeds with 188 tests
- [x] `FLASH.exe` artifact and permanent SP1 ZIP are produced and hash-verified
- [x] Run the packaged executable on Windows 11 build 26200
- [x] Confirm configuration, log, self-check, and registry persistence
- [x] Confirm the obsolete desktop scheduled sync is absent or disabled
- [x] Confirm installer, updater, shortcut, and full safe-status display
- [x] Record clean-account/another-PC verification as user-deferred, not passed
- [x] Resolve the final real-desktop entry without overwriting the existing junction
- [x] Verify read-only capture while partially covered, non-foreground, and minimized
- [x] Configure the user-confirmed fail-closed anonymous Flash window identity
- [x] Build and verify the fingerprint-enabled Windows package on the target desktop
- [x] Add a repeatable, read-only 14-window identity/capture verifier
- [x] Build and verify the packaged `--verify-target-desktop` mode
- [ ] Complete safe-input, reconnect, and player-control acceptance
  - [x] User-approved `B` and `C` background input pass 14/14 live windows
  - [x] One real disconnected window completes forced-login recovery and returns
    the desktop to 14/14 connected windows
  - [x] Select the recent-login route dynamically; line 7 passes shifted-name
    multi-window recognition and background click progression
  - [x] Preserve reconnect context and one-minute retry timing across restarts
  - [x] Verify foreground-only B sends to 1, skips 13, and visibly opens backpack
  - [x] Verify foreground/background and minimized-input modes
  - [x] Finish a stable multi-window reconnect run at 14/14 connected
  - [ ] Verify the current packaged build and player control outside automation
- [ ] Merge only after approval, then verify current `main` and `release/latest`
- [ ] Complete final target-desktop user acceptance
- [ ] Complete final SP1 verification checklist

Important scope note:

The latest released executable remains an SP1 engineering verification
application. Current unreleased source now includes the confirmed `B`/`C`
safe-input controller and forced-login reconnect flow. All three input policies,
line 7 dynamic selection, cross-restart retry context, stacked post-login
popups, minimized-window reconnect, and a final 14/14 connected state are
live-proven. The current packaged build, additional failure variants, and
player control outside automation remain open. Clean-account/another-PC
acceptance was explicitly deferred by the user on 2026-07-26 and is not
claimed as passed.
Player-visible role mapping remains paused.

## SP2 — Product Design

Existing SP2 design is preserved separately. No new SP2 implementation starts
until every required SP1 gate above has direct evidence.
