# Repository visuals

- `motion-studio-banner.svg`: editable, code-native brand illustration. Its rotor and traces are decorative, not hardware measurements.
- `motion-studio-banner.png`: browser-rendered PNG export of the same banner for reuse where SVG is unsupported.
- `showcase-overview.png`: actual full-page Motion Studio screenshot at desktop width, captured from an isolated preview server.
- `position-tracking.png`: actual graph-panel screenshot of simulated relative travel, 32,000 units at 10 RPM, including the target and simulated measured response.
- `current-response.png`: actual graph-panel screenshot of a simulated 150 mA torque-current command with a three-second timeout.

These images demonstrate the interface, not physical motor performance. The README labels their simulation source. No live hardware commands were issued to capture them.

To refresh captures, run `python -m showcase --preview --port 8423`, connect `PREVIEW`, and use the real UI controls. Capture the browser or graph panel at 1440 CSS pixels wide while the last 12 seconds still contain the test. Keep simulation labels/captions with any published graph. Do not edit traces to imply measured performance.
