# Changelog

## SP2 cumulative engineering — 2026-07-26

- Established `sp2/completion-2026-07-26` from accepted SP1 commit `64ecfc2`.
- Added an independent SP2 delivery area without duplicating the cumulative
  source tree.
- Ported the first SP2 Brain batch in `f41e708`: characters, groups,
  activities, three-state progress, Taipei daily reset, atomic progress
  persistence, workspace state, and group-card data/lifecycle/history rules.
- 96 affected tests and compilation of `domain/`, `workspace/`, and `cards/`
  passed.
- Ported the second pure SP2 service batch in `4d63733`: activity-progress
  orchestration, retained card history, card/history coordination, and immutable
  card view state.
- 108 affected core/service tests, compilation of the five new service/view
  modules, and `git diff --check` passed.
- Kept `main.py` registration for a separate batch so the accepted SP1 bootstrap
  is patched deliberately instead of being overwritten from the integration
  branch.
- Registered the cumulative SP2 stores and services in the accepted SP1
  bootstrap in `ae76d64`, including managed activity-progress/card-history
  paths and shared card coordinator/view-state dependencies.
- 51 registration, service, SP1 bootstrap, and self-check regression tests plus
  cumulative Python compilation and `git diff --check` passed.
- Ported atomic character-profile persistence and stable-identity read-only
  character views in `3bb6ac4`; registered both in the cumulative bootstrap.
- 31 character, registration, SP1 bootstrap, and self-check regression tests
  passed without adding character UI or frozen life-soul fields.
- Ported immutable per-character soul-stone records, atomic persistence, and
  transactional query/set/clear services in `acee84c`; registered them on
  managed data paths.
- 36 soul-stone, registration, SP1 bootstrap, and self-check regression tests
  passed without adding the SP3 editor or frozen life-soul data.
- Added immutable character-detail snapshots and stable-identity selection
  commands in `be94642`; registered the detail service in the cumulative
  bootstrap.
- Removed the integration branch's life-soul dependency from character details
  to honor the current frozen-domain boundary; 52 related tests passed.
- Added transactional card display-time updates, change notifications, and a
  scheduler-agnostic expiry monitor in `04bdfc0`; registered settings without
  wiring any SP3 window or overlay.
- 53 card and SP1 bootstrap/self-check regression tests passed.
- Added a read-only SP1 target-window fact to SP2 runtime-state EventBus flow in
  `d359682`, stripping handles, process data, paths, rectangles, pixels, and raw
  adapter details.
- 32 target-window event, EventBus, and SP1 bootstrap/self-check regression
  tests passed with zero input calls.
- No SP3 UI, game input, player-visible registry mapping, or frozen resource
  area was added.

## SP1 0.1.3 independent local acceptance — 2026-07-26

- Added `B`/`C`-only Windows input synchronization with foreground-only,
  foreground/background, and all-including-minimized policies.
- Added exact 14-window identity preflight and fail-closed per-window isolation.
- Added 10 full reconnect reference images plus 2 route-digit templates,
  fail-closed screen recognition, automatic forced-login recovery,
  approximately 10-second force-login waiting, and unlimited failed retries at
  60-second intervals.
- Added the user-confirmed post-login Auto Dungeon window as a third independent
  popup with its own title guard and close point.
- Suppressed duplicate clicks on an unchanged actionable screen for 60 seconds;
  a recognized screen transition still advances immediately.
- Restricted post-login popup closing to a controller-started login/reconnect
  session; matching popups opened manually during normal play are observe-only.
- Added recent-login route selection instead of a fixed top-line click. Route
  text is located by its stable prefix so character-name length and window size
  do not shift the digit out of the detector.
- Persisted pending reconnect context, controller-started popup eligibility, and
  retry deadlines by anonymous launch fingerprint across process restarts.
- Live background `B` and `C` tests each passed 14/14 windows with individual
  visual confirmation.
- One real disconnected window recovered through confirm, forced login,
  character entry, and post-login popup close; the final scan was 14/14
  connected with zero unknown or failed windows.
- Foreground/background B sent to 12/12 non-minimized windows and skipped the
  2 test-minimized windows; all-including-minimized C sent to 14/14 while every
  game window remained minimized for the two-second evidence hold.
- A later run recovered all 14 simultaneously disconnected windows, including
  7 minimized instances, handled stacked Activity and Auto Dungeon windows, and
  ended at 14 connected with zero unknown, failed, or actionable windows.
- Tightened popup/progress thresholds and changed recognition to choose the best
  per-template-valid match, preventing stale popup and progress imagery from
  masking a valid connected-game match.
- Commit `28110e6e4857514ee0c99cccacaa2acd5c7b9de7` passed 278 tests,
  compilation, source self-check, and diff validation.
- Built the independent `FLASH-SP1-Windows-0.1.3-28110e6-snapshot` package.
  Packaged 8/8 self-check, manifest verification, ZIP extraction verification,
  and two GUI launch/normal-close cycles passed with zero residual processes.
- `FLASH.exe` SHA-256:
  `8948e67265929f8e8953afb23c56c09d439aa94696abf2f716702d610e140435`.
- ZIP SHA-256:
  `73cc0d59844acb4f0e6e043b55501d2b0d1ab6f639d69a03bc728b6588809d4a`.
- This independent SP1 snapshot is locally accepted. Clean-account testing was
  explicitly deferred to another PC and is not claimed passed; `main` and
  `release/latest` remain unchanged.
- GitHub Actions run #130 for acceptance-doc commit `8d58315` passed the full
  278-test Windows workflow and produced artifact `8625523051` with digest
  `caecb7ad0758486007c36c0be2f990e5facc819d642b1b8ea3e7a1affe652fc8`.
  `release/sp1` advanced to `4575cfc`; `release/latest` was skipped.

## SP1 0.1.2 — 2026-07-25

### Latest verified repeatable target-desktop release

- Source: `f47e132d1d3677af9fa2952ee6dcd99e0d073918`
- Release source: `0bd575c6d049abe686ca539d949e246010a11612`
- GitHub Actions: run #129
- Artifact: `8617356997`
- Tests: 209 passed
- Source and packaged self-checks: 8/8 passed
- Added `scripts/verify_target_desktop_sp1.py` and packaged
  `FLASH.exe --verify-target-desktop` mode.
- The real desktop passed 14/14 individual identity selections and 14/14
  non-blank captures with zero wrong-window selections.
- Reports contain aggregate counts only and persist no pixels, process IDs,
  window handles, fingerprints, launcher arguments, or input.
- Source, unpacked package, and installed `--verify-target-desktop` runs all
  passed 14/14 selection and capture acceptance.
- Published only to `release/sp1`; the permanent ZIP SHA-256 is
  `ab02bf2b5d8d8eba3fcf5278329cf4c23741da360b2b824c40ce8473f762629f`.

### Latest verified anonymous-window-identity release

- Source: `2fae8bfba8b13a3fe05ba67f6af05d37cfc4a9ee`
- Release source: `49ee6681627fe3b94f94c19fabc1dc193e4f20e2`
- GitHub Actions: run #128
- Artifact: `8617136485`
- Tests: 200 passed
- Source and packaged self-checks: 8/8 passed
- Added secret-safe SHA-256 fingerprints for identical Flash windows.
- Requires exactly one launcher identity per window; invalid, missing, or
  duplicate identities fail closed instead of choosing a substitute.
- A read-only target-desktop probe confirmed 14 windows, 14 processes, and
  14 distinct fingerprints. It emitted no raw arguments and sent no input.
- Published only to `release/sp1`; the permanent ZIP SHA-256 is
  `40838dfff48e858079a479242aa670743e8f08b5ec0c5f3e7d73a5f01bd3852d`.
- The fixed updater, packaged identity selection, installed GUI launch, normal
  close, and no-residual-process checks passed on Windows 11.

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

This historical 0.1.2 line remained an engineering verification stage. For the
0.1.3 independent local acceptance, clean-account testing is deferred to
another PC and is not claimed passed.
