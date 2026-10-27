# Strata Board Harness

This is a standalone project. Keep it portable to Ubuntu with Python 3.10+.
Do not import the Mac benchmark manager or embed its paths, credentials or identity.

Run `python3 -m unittest discover -s tests -v` after functional changes.
Use local fake services for tests. Keep the current AttentiveOS benchmark untouched.
The supervisor must tolerate an unavailable model without exiting or losing state.
Board identities, task versions, message receipts and worker process ownership must
remain explicit. Lease expiry alone is never proof that a worker is dead.

The daemon, own-identity Board coordinator and selected coding/validation adapters
are wired. Treat fake-service and readiness checks separately from real pilot and
production acceptance. Only the manager-selected pilot is authorized. Never weaken
tests to make it pass. The Git repository is git@github.com:Blackoutburst/Strata-Harness.git (branch main).
