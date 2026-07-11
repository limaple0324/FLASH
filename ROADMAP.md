# FLASH Roadmap

## SP1 — Core Foundation

Current target: verified SP1 foundation and Windows test build

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
- [x] Desktop-to-GitHub main-branch sync scripts

Remaining SP1 verification work:

- [ ] Confirm GitHub Actions Windows run succeeds
- [ ] Confirm FLASH.exe artifact is produced
- [ ] Run Windows executable on the user's computer
- [ ] Confirm configuration and log persistence after packaging
- [ ] Confirm desktop scheduled sync is registered and updating main
- [ ] Complete final SP1 verification checklist

Important scope note:

The current executable is an SP1 engineering verification application. It
verifies startup, persistence, service registration, event flow, and stable
extension boundaries. Concrete game-specific recovery and reconnect adapters
must be integrated and validated before those behaviors can be called complete.

## SP2 — Product Design

Product design continues separately and should be integrated after SP1 foundation is verified.
