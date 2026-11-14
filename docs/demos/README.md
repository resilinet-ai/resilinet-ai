# Demos

Recorded demonstrations of working RAI features. Each one is early evidence that the artefact exists and runs.

Videos are **not** committed: pre-commit rejects files over 500 KB, and a video would add to every clone forever. Attach each video to its pull request instead (GitHub accepts files up to 100 MB). Then add a row below with the link and the video's SHA-256, so anyone can check that the file they watch is the one recorded.

| Date | Demo | What it shows | Video (link) | SHA-256 |
| --- | --- | --- | --- | --- |
| _pending_ | First runnable slice (B-14) | `make demo`: SandboxBank generates two 100k-row batches; the injector re-sends 100 rows; `rai validate` passes batch 1 (exit 0) and fails batch 2 with exactly those 100 rows (exit 1); the JSON report is byte-identical across runs | _to add_ | _to add_ |

## Recording the first runnable slice (about 2 minutes)

1. Open a terminal in the repository root and make the font large enough to read in a video.
2. Start recording. On Windows, **Win + Alt + R** (Xbox Game Bar) records the active window.
3. Say what you are about to show, then run `make demo`. It takes about 15 seconds.
4. Point out the four results: PASS with exit code 0; FAIL naming `unique_across_batches:txn_id` with 100 rows and exit code 1; the first rows; and the five ticked checks at the end.
5. Stop recording and get the video's hash: `certutil -hashfile <video>.mp4 SHA256` (Windows) or `sha256sum <video>.mp4`.
6. Drag the video into a comment on the B-14 pull request, copy its link, and fill in the row above.
