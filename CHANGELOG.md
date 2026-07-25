# Changelog

## SP1 0.1.2 — 2026-07-25

### Latest locally verified anonymous-window-identity source

- Source: `2fae8bfba8b13a3fe05ba67f6af05d37cfc4a9ee`
- Local tests: 200 passed
- Source self-checks: 8/8 passed
- Added secret-safe SHA-256 fingerprints for identical Flash windows.
- Requires exactly one launcher identity per window; invalid, missing, or
  duplicate identities fail closed instead of choosing a substitute.
- A read-only target-desktop probe confirmed 14 windows, 14 processes, and
  14 distinct fingerprints. It emitted no raw arguments and sent no input.
- Windows packaging and `release/sp1` publication for this source are pending.

### Latest verified real-background-capture release

- Source: `960bacb7260ea33f59c0724b219472fcbc36924e`
- GitHub Actions: run #127
- Tests: 188 passed
- Source and packaged self-checks: 8/8 passed
- Captured valid non-blank frames from 14 real Flash windows, including
  non-foreground, partially/fully covered, and minimized states.
- Used saved normal bounds for minimized capture instead of accepting the
  353 by 39 minimized shell as game content.
- Removed the stale `輔 V0.2` shortcut name from updater completion text.
- Published only to `release/sp1`; `release/latest` remained unchanged.
- Permanent artifact:
  `FLASH-SP1-Windows-0.1.2-960bacb-sp1-release.zip`
- ZIP SHA-256:
  `14a6adba241defb0415d92ca66a48d3e1980c05204e5c7f3f4cfadeda13abe7d`

### Earlier verified real-desktop-entry release

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
