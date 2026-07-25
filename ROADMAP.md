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

- [x] GitHub Actions Windows run #125 succeeds with 186 tests
- [x] `FLASH.exe` artifact and permanent SP1 ZIP are produced and hash-verified
- [x] Run the packaged executable on Windows 11 build 26200
- [x] Confirm configuration, log, self-check, and registry persistence
- [x] Confirm the obsolete desktop scheduled sync is absent or disabled
- [x] Confirm installer, updater, shortcut, and full safe-status display
- [ ] Run the packaged executable under a clean Windows user account
- [x] Resolve the final real-desktop entry without overwriting the existing junction
- [ ] Configure the user-confirmed real Flash window identity
- [ ] Complete real-game capture, safe-input, reconnect, and player-control acceptance
- [ ] Merge only after approval, then verify current `main` and `release/latest`
- [ ] Complete final target-desktop user acceptance
- [ ] Complete final SP1 verification checklist

Important scope note:

The current executable is an SP1 engineering verification application. It
verifies startup, persistence, service registration, event flow, and stable
extension boundaries. Concrete game-specific recovery and reconnect adapters
must be integrated and validated before those behaviors can be called complete.
Those items remain paused until the window identity, harmless input, login,
disconnect, reconnect, and failure conditions are confirmed by the user.

## SP2 — Product Design

Existing SP2 design is preserved separately. No new SP2 implementation starts
until every required SP1 gate above has direct evidence.
