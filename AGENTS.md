# PD42S1 Showcase

Customer-facing scope: velocity, current-based torque, absolute/relative position, zero, encoder calibration, homing, stall release, and live graphs. Keep advanced engineering controls in `../pd42s1_mdm`.

## CodeGraph

This repository has its own local `.codegraph/` index when initialized. Prefer CodeGraph structural queries before editing existing symbols. Pass this checkout's absolute root as `projectPath`; the engineering app has a separate index. Use native search for literal strings or already-open files. On a fresh clone, run `codegraph init -i .`; rebuild with `codegraph index .` if required. Generated index files must not be committed.

## Development

- Use `python -m unittest discover -s tests -v` and preview mode for repeatable verification.
- Do not actuate real hardware without current user authorization. Preview never opens serial devices.
- Validate all command values before enabling. Always stop/disable on test end, failure, disconnect, and shutdown; do not report disable as successful without read-back verification.
- Calibration acknowledgement is not completion. Homing timeout is not completion. Do not fabricate either status.
- Preserve the shared core's serial-operation arbitration and enable/disable compatibility logic.
- Keep dependencies local, the server loopback-only, controls keyboard-accessible, and user-entered values separate from live telemetry.
