# Changelog

## SP1 0.1.2 — 2026-07-25

### Latest verified safe-status release

- Source: `03db0624fb64e7b5997502558914a0f706da7b79`
- GitHub Actions: run #124
- Tests: 184 passed
- Source and packaged self-checks: 8/8 passed
- Added complete read-only self-check, target-window, background capability,
  registry, disabled-input, and log-location display to the packaged window.
- Serialized background capability states as stable player-readable values.
- Published only to `release/sp1`; `release/latest` remained unchanged.
- Permanent artifact:
  `FLASH-SP1-Windows-0.1.2-03db062-sp1-release.zip`
- ZIP SHA-256:
  `e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`

### Earlier verified releases

- `cee31ac`: complete first installer, rollback, and one shortcut; run #123.
- `f419e07`: dedicated SP1 transactional update channel; run #120.
- `689a186`: Windows engineering snapshot and hash compatibility; run #118.

SP1 remains an engineering verification stage until the clean-account,
real-desktop, confirmed real-game, current-`main`, and final user-acceptance
gates are complete.
