# SP1 Verification Checklist

This document is the delivery gate for FLASH SP1. A feature is not considered complete merely because code or a workflow file exists.

Scope note (2026-07-26): SP1 completion work is restricted to
`sp1/completion-2026-07-25`, based on `main@538bdbc`. The latest verified source
artifact commit is
`28110e6e4857514ee0c99cccacaa2acd5c7b9de7`. It passed 278 local tests,
Python compilation, 8/8 source self-checks, all three input policies, and a
final 14/14 reconnect run. The
earlier `5db5b5f` local baseline passed 171 tests, Python compilation, 8/8
source self-checks, and `git diff --check`; the subsequent hash-compatibility
fix passed the 24 affected
updater/release-verifier tests and one focused regression test.

GitHub Actions run #118 passed compilation, the complete test suite, source and
packaged self-checks, Windows EXE creation, bundle layout, metadata, hashes, and
artifact upload. The `689a186` snapshot also passed its bundled verifier under
local Windows PowerShell 5.1. Its ZIP SHA-256 matches the GitHub artifact digest:
`30aca05eb2e8c84f10c58a86cc34af89cda8559b31989b8b6cb266e007b231fa`.
On Windows 11 build 26200, the downloaded artifact also passed two GUI launches,
normal close/reopen, isolated config/log/registry persistence, 8/8 packaged
self-check results, and no-console verification. This used the current Windows
account with isolated `APPDATA`/`LOCALAPPDATA`, not a clean new user account, and
did not exercise a real game flow. SP1 is therefore still not formally complete.

Current SP1 completion work added target-directed B/C input, dynamic
recent-route selection, anonymous-fingerprint reconnect persistence, and live
multi-window evidence. All 278 tests, Python compilation, source self-check,
all three input policies, and final 14/14 reconnect acceptance pass. The
independent SP1 0.1.3 Windows package passed 8/8 packaged self-checks, manifest
verification, ZIP extraction verification, and two GUI launch/normal-close
cycles with zero residual processes. Independent local SP1 acceptance is
complete. The user deferred clean-account/another-PC testing on 2026-07-26,
so it is not a local packaging blocker and is not claimed passed. `main` and
`release/latest` remain unchanged pending separate approval.

GitHub Actions run #120 for `f419e07` passed the complete workflow and published
the SP1-only release to `release/sp1`. The permanent ZIP SHA-256 matches artifact
`8616031855`:
`746fc2c630b77e7d7e3d2db87089716cfe5ebf0fda37e5cf9010d264d5fd6b66`.
On the target Windows 11 computer, its real `更新輔.cmd` successfully resolved
`release/sp1`, downloaded one fixed release commit, passed pre-install
verification, applied the transaction, and passed post-install verification.

GitHub Actions run #123 for `cee31ac` passed all 183 tests and published the
complete installer/updater bundle. Its permanent ZIP SHA-256 matches artifact
`8616207872`:
`40af7b7c6a8e8ab8c768cdf6aa5961cb82f1a28f6c7075204b316adbec52053a`.
The real installer then passed source, staged, and installed bundle verification
on Windows 11 and created exactly one isolated `輔.lnk` with the expected target,
working directory, and executable icon.

GitHub Actions run #124 for `03db062` passed 184 tests, source and packaged
8/8 self-checks, Windows EXE creation, bundle verification, artifact upload,
and SP1-only publication. The permanent ZIP SHA-256 matches artifact
`8616418914`:
`e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`.
On Windows 11 build 26200, the same-source packaged window displayed the full
self-check details, conservative target-window state, all three background
capability states, registry state, disabled-input notice, and isolated log path.

GitHub Actions run #125 for `ac1223a` passed all 186 tests, source and packaged
8/8 self-checks, Windows EXE creation, bundle verification, artifact upload,
and SP1-only publication. The permanent ZIP SHA-256 matches artifact
`8616653423`:
`3854f49733dcd5d23f9a8452d0390f5aca31c7aaed1c89bc505f45240d38ff63`.
On the real Windows 11 desktop, the transactionally installed package preserved
the existing `輔` project junction, created only `啟動輔.lnk`, passed target,
working-directory, icon, EXE-hash, GUI launch, and normal-close checks, and left
no FLASH process behind.

GitHub Actions run #127 for `960bacb` passed all 188 tests, source and packaged
8/8 self-checks, Windows EXE creation, bundle verification, artifact upload,
and SP1-only publication. The permanent ZIP SHA-256 matches artifact
`8616874795`:
`14a6adba241defb0415d92ca66a48d3e1980c05204e5c7f3f4cfadeda13abe7d`.
Read-only target-desktop probing covered all 14 matching Flash windows without
persisting images or sending input: 13 were non-foreground, four partially
covered, one fully covered, and five minimized. All returned non-blank frames;
the minimized capture fix used the saved normal window bounds and returned
911/916 by 629 frames instead of the misleading 353 by 39 minimized shell.
The installed SP1 release was transactionally updated to the same source commit.

The user approved anonymous launcher-argument SHA-256 fingerprints for identical
Flash windows. Commit `2fae8bf` implements this without returning raw shortcut
arguments or process command lines to Python. Invalid, missing, non-unique, or
duplicate identities fail closed. The real desktop probe found 14 windows,
14 process IDs, and 14 distinct fingerprints, and sent no input. The Windows
package and `release/sp1` verification for source `49ee668` passed in GitHub
Actions run #128 with artifact `8617136485`. The ZIP SHA-256 is
`40838dfff48e858079a479242aa670743e8f08b5ec0c5f3e7d73a5f01bd3852d`.
Packaged unique selection, transactional update, GUI launch, normal close, and
no-residual-process checks passed on the target Windows 11 desktop.

Every new item must be confirmed as discussed before implementation. The
player-viewable window-registry area and the five frozen resource areas remain
paused. SP2 and SP3 work does not resume until the prior delivery stage is complete.

## A. Repository and delivery update

- [x] GitHub default branch is `main`.
- [x] SP1 completion uses a dedicated branch based on `main`.
- [x] SP1 has an independent delivery area and status file.
- [x] The player-facing update entry is the single `更新輔.cmd`.
- [x] Restrict `release/latest` publication to an approved `main` push.
- [x] Give the independent SP1 build an update source that cannot advance to
  SP2, SP3, or the complete integrated delivery.
- [x] Confirm the obsolete scheduled Git sync task is absent or disabled.
- [x] Implement same-commit, full-payload verification and transactional update
  with rollback and failure-injection coverage.
- [x] Run `更新輔.cmd` on the target computer and verify the installed build.
- [x] Implement and verify a complete first installer with rollback and one
  executable-backed shortcut.
- [x] Verify the real-desktop installer preserves the existing `輔` junction
  and creates a distinct, working `啟動輔.lnk`.

## B. Core startup

- [x] Persistent config and log paths are created.
- [x] Core services are registered through `AppContext`.
- [x] Bootstrap returns structured startup status.
- [x] Startup exceptions are logged and shown to the user.
- [x] Packaged executable supports non-GUI `--self-check` verification.
- [x] CI verifies packaged config, log, and self-check report creation.
- [x] Record the user's decision to defer clean-account/another-PC verification;
  do not claim that external gate passed.

## C. SP1 self-check

- [x] Paths check.
- [x] Configuration check.
- [x] Logger write check.
- [x] Event bus delivery check.
- [x] Recovery boundary check.
- [x] Smart Reconnect boundary check.
- [x] External adapter boundary check.
- [x] Detailed self-check result formatting is covered by source tests.
- [x] Corrupt configuration is preserved and rebuilt automatically.
- [x] Confirm detailed results display correctly in the packaged application on
  the target Windows 11 desktop.
- [x] Confirm all checks pass in the packaged executable on the target desktop.

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
- [x] Background capture result model and JSON report output are covered by
  source tests.
- [x] Confirm the result is shown correctly in the packaged SP1 window on the
  target Windows 11 desktop.
- [x] Configure the approved anonymous Adobe Flash Player window identity.
- [x] Reject invalid, missing, or duplicate identities without selecting a
  substitute window or exposing raw launcher data.
- [x] Verify the fingerprint-enabled packaged application on Windows 11.
- [x] Run a three-cycle installed read-only stability baseline with 14/14
  identity selection, 14/14 capture, and zero wrong-window selections.
- [x] Verify background capture while the game is partially covered.
- [x] Verify background capture while the game is not foreground.
- [x] Verify background capture while the game is minimized.
- [x] Add a user-approved harmless background-input probe (`B` and `C` only).
- [x] Verify non-foreground background `B` and `C` input on all 14 target
  windows, including individual visual confirmation.
- [x] Implement three explicit input policies: foreground only, foreground plus
  non-minimized background, and all validated windows including minimized.
- [x] Verify foreground-only B delivery sends to exactly one live target and
  visually opens that target's backpack while 13 targets are skipped.
- [x] Finish live acceptance of all three input policies and restore every game
  window to its normal view afterward.
- [ ] Verify minimized background input on the target desktop.
- [x] Implement and test one real reconnect sequence: 13 connected plus 1
  disconnected, then confirm, forced login, character entry, popup close, and
  final 14/14 connected with zero unknown or failed windows.
- [x] Select the route shown in recent-login information instead of always
  selecting the top route; line 7 is live-proven with shifted character names.
- [x] Persist pending reconnect, controller-started popup context, and 60-second
  retry deadlines by anonymous fingerprint across controller restarts.
- [x] Finish a stable multi-window reconnect run at final 14/14 connected after
  the currently observed repeated server disconnects.
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
- [x] Local baseline passes 171 tests at `5db5b5f`; the subsequent
  hash-compatibility change passes all 24 affected tests and its regression test.
- [x] Win32 adapter and transactional updater received independent focused
  reviews with no remaining blocker.
- [x] GitHub Actions run #118 for `689a186` passes the complete workflow.
- [x] GitHub Actions run #120 for `f419e07` passes the complete workflow and
  publishes only to `release/sp1`.
- [x] GitHub Actions run #123 for `cee31ac` passes all 183 tests and publishes
  the complete installer/updater bundle only to `release/sp1`.
- [x] GitHub Actions run #124 for `03db062` passes all 184 tests, publishes only
  to `release/sp1`, and its packaged status window passes Windows 11 display
  acceptance.
- [x] GitHub Actions run #125 for `ac1223a` passes all 186 tests, publishes only
  to `release/sp1`, and its collision-safe shortcut passes real-desktop
  installation and launch acceptance.
- [x] GitHub Actions run #127 for `960bacb` passes all 188 tests, publishes only
  to `release/sp1`, and its read-only capture passes covered, non-foreground,
  and minimized real-Flash acceptance.
- [x] Local `2fae8bf` source passes all 200 tests, compilation, 8/8 source
  self-checks, and 14-window one-to-one anonymous identity acceptance.
- [x] GitHub Actions run #128 for `49ee668` passes all 200 tests, packages and
  publishes only to `release/sp1`, and passes installed identity/GUI acceptance.
- [x] Add repeatable target-desktop identity and capture integration verification.
- [x] Add reconnect integration after confirming 10 full reference screens
  plus 2 route-digit templates, forced
  login after disconnect, an approximately 10-second wait, 60-second unlimited
  failed retries, known popup close actions, and fail-closed unknown screens.

### 2026-07-25 live game evidence

- Background `B` opened the backpack on 14/14 exact target windows; each capture
  was checked individually, and the windows were restored.
- Background `C` opened character information on 14/14 exact target windows;
  each capture was checked individually, and the windows were restored.
- A read-only scan identified 13 connected windows and exactly 1 disconnected
  window. Only that disconnected window received reconnect clicks.
- The source controller completed confirm, forced login, approximately
  10 seconds of waiting, character entry, and known post-login popup close.
  The final scan reported 14 connected, 0 unknown, 0 failed, and 0 further
  actions.
- A later live session displayed up to 9 line-selection windows at once. The
  fixed digit crop safely withheld 2 shifted line-7 screens; prefix-relative
  recognition corrected them, and background route-7 clicks advanced multiple
  clients without requiring the Flash window to be foreground.
- The persistent version-1 runtime state survived verifier restarts, retained
  one-minute suppression and popup-close context, and contained no handles,
  process IDs, raw arguments, character names, or captured pixels.
- Foreground-only B passed exact 14-window preflight, sent to 1, skipped 13, and
  visibly opened the selected window's backpack.
- Foreground/background B temporarily minimized 2 exact windows, sent to the
  remaining 12/12, skipped 2, and visually confirmed the expected open/closed
  backpack split before restoring all windows and closing the 12 backpacks.
- All-including-minimized C held all 14 windows minimized for 2 seconds, sent
  to 14/14 with zero skips or failures, visibly opened character information,
  and repeated the same test to close it and restore normal gameplay.
- A later server-wide run began with all 14 disconnected, including 7 minimized
  windows. It preserved foreground/minimized state while advancing all clients,
  handled stacked Activity and Auto Dungeon windows independently, and ended at
  14 connected, 0 unknown, 0 failed, and 0 actionable.
- Current packaged-build verification and final local player acceptance passed
  for the independent 0.1.3 snapshot. Clean-account/another-PC verification was
  explicitly deferred by the user on 2026-07-26; it is not a local packaging
  blocker and is not claimed as passed.

## F. Build and delivery

- [x] PyInstaller specification exists.
- [x] Windows workflow installs dependencies and runs tests.
- [x] Workflow verifies `dist/FLASH.exe` exists.
- [x] Workflow runs packaged `--self-check` mode.
- [x] Workflow verifies persistent config, log, and JSON report creation.
- [x] Workflow records version, commit, build time, and SHA-256.
- [x] Workflow uploads a complete verification bundle.
- [x] Older `3c92253` engineering snapshot passed its bundled release verifier
  and packaged `--self-check`.
- [x] Verify the `689a186` bundle, metadata, manifest, hashes, artifact digest,
  bundled Windows PowerShell 5.1 verifier, and cloud packaged `--self-check`.
- [x] Verify and permanently save the `f419e07` SP1-only release, its artifact
  digest, `release/sp1` identity, and real target-computer updater flow.
- [x] Verify and permanently save the `cee31ac` complete installer/updater
  release and its isolated Windows 11 first-install/shortcut flow.
- [x] Verify and permanently save the `ac1223a` real-desktop-entry release and
  its junction-preserving first-install/shortcut flow.
- [x] Verify and permanently save the `960bacb` real-background-capture release,
  including its target-desktop updater and minimized normal-bounds fix.
- [x] Verify and permanently save the `49ee668` anonymous-window-identity
  release, including one-to-one packaged selection and target-desktop update.
- [x] Build and verify the `f47e132` packaged target-desktop verification mode
  in `0bd575c` / GitHub Actions run #129.
- [x] Preserve current `main` and `release/latest` unchanged; their future
  merge/publication verification requires separate approval and is not a gate
  for the independent SP1 snapshot.
- [x] Download and launch the current artifact on Windows 11 build 26200 with an
  isolated data profile.
- [x] Confirm config, log, self-check, and window-registry persistence after
  closing and reopening.
- [x] Confirm no console window appears.
- [x] Complete local user acceptance on the target desktop for the independent
  SP1 snapshot.

## Delivery states

- **Engineering foundation:** core boundaries and diagnostics exist.
- **Engineering verification build:** executable starts and self-check passes, but real game adapters may still be absent.
- **Player test build:** concrete target-window, recovery, and reconnect behavior is integrated and tested.
- **SP1 complete:** all required checklist items are checked and the target Windows desktop acceptance test passes.

## Independent delivery records

- SP1 delivery area: `deliverables/sp1/`
- SP1 status file: `docs/01_輔_SP1_基礎系統_狀態與驗收.md`
- Overall project file: `docs/00_輔_專案完整總整理與未來路線圖.md`
- Latest verified engineering snapshot:
  `FLASH-SP1-Windows-0.1.2-689a186-snapshot.zip`, source
  `689a186e5cd3e93e72e98c25e2840deb49e0a637`, GitHub Actions run
  `30144187526`, artifact `8615455764`, SHA-256
  `30aca05eb2e8c84f10c58a86cc34af89cda8559b31989b8b6cb266e007b231fa`.
- Permanent evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-689a186-snapshot.md`.
- Latest SP1-only release:
  `FLASH-SP1-Windows-0.1.2-f419e07-sp1-release.zip`, source
  `f419e07ebdef5e4836e6229df082c6222b2ef551`, GitHub Actions run
  `30146010560`, artifact `8616031855`, publication commit
  `56c815680c9f8cc281f1674f757a3c11f79beba0`, SHA-256
  `746fc2c630b77e7d7e3d2db87089716cfe5ebf0fda37e5cf9010d264d5fd6b66`.
- Latest release evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-f419e07-sp1-release.md`.
- Latest complete installer/updater release:
  `FLASH-SP1-Windows-0.1.2-cee31ac-sp1-release.zip`, source
  `cee31ac2124ba3584f3d278d5dd67330d8cb312a`, GitHub Actions run
  `30146543198`, artifact `8616207872`, publication commit
  `65e58a70543d082cbe96c207e4cda5bf5ca22360`, SHA-256
  `40af7b7c6a8e8ab8c768cdf6aa5961cb82f1a28f6c7075204b316adbec52053a`.
- Latest complete release evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-cee31ac-sp1-release.md`.
- Latest complete safe-status release:
  `FLASH-SP1-Windows-0.1.2-03db062-sp1-release.zip`, source
  `03db0624fb64e7b5997502558914a0f706da7b79`, GitHub Actions run
  `30147184671`, artifact `8616418914`, publication commit
  `8c0c61bae3a1c3f97b8794162c7ba88a415a2e94`, SHA-256
  `e470f5c71b5c81a182d8d0532c1cfb2f00385233868735cdc797284fe99f097b`.
- Latest safe-status release evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-03db062-sp1-release.md`.
- Latest real-desktop-entry release:
  `FLASH-SP1-Windows-0.1.2-ac1223a-sp1-release.zip`, source
  `ac1223a45ccfd12ec3096ccd8fd7933cdbb2bfe3`, GitHub Actions run
  `30147951154`, artifact `8616653423`, publication commit
  `a59bc3c2c0c5dcb731bee5971660fb391fbcf5b3`, SHA-256
  `3854f49733dcd5d23f9a8452d0390f5aca31c7aaed1c89bc505f45240d38ff63`.
- Latest real-desktop-entry evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-ac1223a-sp1-release.md`.
- Latest real-background-capture release:
  `FLASH-SP1-Windows-0.1.2-960bacb-sp1-release.zip`, source
  `960bacb7260ea33f59c0724b219472fcbc36924e`, GitHub Actions run
  `30148568086`, artifact `8616874795`, publication commit
  `43c068d360dc63f04130660382b38614cc8709de`, SHA-256
  `14a6adba241defb0415d92ca66a48d3e1980c05204e5c7f3f4cfadeda13abe7d`.
- Latest real-background-capture evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-960bacb-sp1-release.md`.
- Latest anonymous-window-identity release:
  `FLASH-SP1-Windows-0.1.2-49ee668-sp1-release.zip`, source
  `49ee6681627fe3b94f94c19fabc1dc193e4f20e2`, GitHub Actions run
  `30149404672`, artifact `8617136485`, publication commit
  `5f6cb418e5109f633d3622b897865c873405c084`, SHA-256
  `40838dfff48e858079a479242aa670743e8f08b5ec0c5f3e7d73a5f01bd3852d`.
- Latest anonymous-window-identity evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-49ee668-sp1-release.md`.
- Latest repeatable target-desktop verification release:
  `FLASH-SP1-Windows-0.1.2-0bd575c-sp1-release.zip`, source
  `0bd575c6d049abe686ca539d949e246010a11612`, GitHub Actions run
  `30150105580`, artifact `8617356997`, publication commit
  `f1b83ee7787ebc9498556d8112f15924e3aa8035`, SHA-256
  `ab02bf2b5d8d8eba3fcf5278329cf4c23741da360b2b824c40ce8473f762629f`.
- Latest repeatable target-desktop evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.2-0bd575c-sp1-release.md`.
- Latest independent SP1 local-acceptance snapshot:
  `FLASH-SP1-Windows-0.1.3-28110e6-snapshot.zip`, source
  `28110e6e4857514ee0c99cccacaa2acd5c7b9de7`, version `0.1.3`,
  `FLASH.exe` SHA-256
  `8948e67265929f8e8953afb23c56c09d439aa94696abf2f716702d610e140435`,
  ZIP SHA-256
  `73cc0d59844acb4f0e6e043b55501d2b0d1ab6f639d69a03bc728b6588809d4a`.
- Latest independent SP1 evidence:
  `C:\Users\USER\Documents\輔\SP1成品\FLASH-SP1-Windows-0.1.3-28110e6-snapshot.md`.
- The latest SP1 artifact record must include branch, full commit, version,
  tests, self-checks, SHA-256, build source, Windows evidence, and publication
  state.
