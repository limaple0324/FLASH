# FLASH Roadmap

## SP1 — Core Foundation

SP1 0.1.3 independent local Windows acceptance is complete. The next target is
the separate SP2 delivery area plus the SP1+SP2 cumulative worktree.

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
- [x] Complete safe-input, reconnect, and player-control acceptance
  - [x] User-approved `B` and `C` background input pass 14/14 live windows
  - [x] One real disconnected window completes forced-login recovery and returns
    the desktop to 14/14 connected windows
  - [x] Select the recent-login route dynamically; line 7 passes shifted-name
    multi-window recognition and background click progression
  - [x] Preserve reconnect context and one-minute retry timing across restarts
  - [x] Verify foreground-only B sends to 1, skips 13, and visibly opens backpack
  - [x] Verify foreground/background and minimized-input modes
  - [x] Finish a stable multi-window reconnect run at 14/14 connected
  - [x] Verify the current packaged build, normal close/reopen, and restored
    player control
- [ ] Merge only after separate approval, then verify current `main` and
  `release/latest` (not part of the independent SP1 snapshot acceptance)
- [x] Complete final target-desktop local user acceptance
- [x] Complete final independent SP1 verification checklist

Important scope note:

The independent SP1 0.1.3 snapshot from `28110e6` has passed all 278 tests,
source/packaged self-checks, all three input policies, route-7 dynamic
selection, cross-restart retry context, stacked post-login popups,
minimized-window reconnect, final 14/14 connected acceptance, verified ZIP
extraction, and two GUI launch/normal-close cycles. Clean-account/another-PC
acceptance was explicitly deferred by the user on 2026-07-26 and is not
claimed as passed. `main` and `release/latest` remain unchanged pending
separate approval.
Player-visible role mapping remains paused.

## SP2 — Product Design

Existing SP2 design is preserved separately. SP2 now starts in its own delivery
area after reconciling only user-confirmed scope from the existing integration
branch.
