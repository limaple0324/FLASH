# Changelog

## SP1 0.1.2 — 2026-07-25

### Latest verified real-desktop-entry release

- Source: `ac1223a45ccfd12ec3096ccd8fd7933cdbb2bfe3`
- GitHub Actions: run #125
- Tests: 186 passed
- Source and packaged self-checks: 8/8 passed
- Preserved an existing desktop item named `輔` and selected the distinct
  `啟動輔.lnk` name on first installation.
- Verified the real Windows 11 desktop shortcut target, working directory,
  icon, EXE hash, GUI launch, normal close, and preserved project junction.
- Published only to `release/sp1`; `release/latest` remained unchanged.
- Permanent artifact:
  `FLASH-SP1-Windows-0.1.2-ac1223a-sp1-release.zip`
- ZIP SHA-256:
  `3854f49733dcd5d23f9a8452d0390f5aca31c7aaed1c89bc505f45240d38ff63`

### Earlier verified safe-status release

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
confirmed real-game, current-`main`, and final user-acceptance
gates are complete.
