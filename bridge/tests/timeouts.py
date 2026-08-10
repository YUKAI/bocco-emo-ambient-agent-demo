"""The deadline used by every ``asyncio.wait_for`` guard in this suite.

These guards are not latency assertions. Each one wraps a wait that should
finish immediately, and exists so that a deadlock fails one test in seconds
rather than hanging the whole run until something kills it. The property under
test is always asserted separately — the concurrency test around the
acknowledgment send, for instance, proves its point with
``assertFalse(release_motion.is_set())`` after the reply has been delivered,
not with how long the wait took.

So the number only has to be larger than the slowest machine that will ever run
this, and it was originally one second, which is not. A GitHub-hosted runner is
a shared two-core box and ran this suite roughly ten times slower than a local
container; a guard tripped there on a test that had passed ninety seconds
earlier in the same job. A flaky check is worse than no check, because it
teaches everyone to re-run red rather than read it.

Thirty seconds costs nothing when nothing is wrong — the waits finish in
milliseconds — and still turns a real deadlock into a failure rather than a
hang.
"""

LIVENESS_TIMEOUT = 30.0
