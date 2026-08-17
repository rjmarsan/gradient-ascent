# Installation and setup

## 1. Install the Python package

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-build.lock
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
```

The two lock files include SHA-256 hashes for every accepted distribution. Review and regenerate them whenever a dependency changes; do not hand-edit hashes.

If `python3` is older than 3.11, select a compatible interpreter explicitly instead of continuing with an unsupported runtime.

On Debian or Ubuntu, an `ensurepip is not available` error means the interpreter lacks venv support. Install the matching OS `python3-venv` package, or create `.venv` with an already trusted `virtualenv` installation; do not install Gradient Ascent into the system Python.

## 2. Install the local Codex plugin

```bash
scripts/install-local-plugin.sh
```

The installer copies only the plugin manifest, skills, license, and README into a local Codex marketplace. It does not copy the Python package, tests, examples, Git metadata, or athlete data.

Restart Codex Desktop or start a new task so the installed skills are discovered.

## 3. Initialize a private athlete workspace

```bash
.venv/bin/gradient-ascent init-workspace ~/code/gradient-ascent-workspace
cd ~/code/gradient-ascent-workspace
```

The plugin checkout and athlete workspace must be different directories. Gradient Ascent refuses to write athlete data inside its own checkout.

In Codex Desktop, add or open this private athlete workspace folder as a separate
project for ongoing coaching conversations. Reuse the existing folder; do not create
another repository or put athlete data in the public Gradient Ascent checkout. Open a
new task after installing the plugin so its coaching skills are available.

Initialization creates:

- prompt-facing workspace instructions
- a `.env` containing only the workspace path
- structured plan and onboarding files
- ignored directories for raw Strava, Garmin, and Apple Health imports
- a workspace-local launcher at `.codex/bin/gradient-ascent`
- a Codex environment action for the Training Center

It does not initialize Git or add a remote.

## 4. Run prompt-driven onboarding

Paste [SETUP_PROMPT.md](SETUP_PROMPT.md) into a new Codex task. Codex should resume from:

```bash
.codex/bin/gradient-ascent onboarding-status --json
```

The flow covers profile, time zone, units, disciplines, experience, availability, constraints, goals, events, plan choice, activity-history choice, and optional recovery imports. Existing answers are preserved.

After onboarding, ask `$coach-advice` questions from the private workspace project,
or use the Training Center's **Ask Coach** button to open a conversation in that same
workspace. Use `$coach-note` to save an approved coaching insight with a link back
to its Codex conversation.

## 5. Import optional local sources

```bash
.codex/bin/gradient-ascent import-strava-export /path/to/strava-export.zip
.codex/bin/gradient-ascent import-activity-recording /path/to/ride.fit
.codex/bin/gradient-ascent import-garmin-export /path/to/garmin-export
.codex/bin/gradient-ascent import-apple-health-export /path/to/export.xml
.codex/bin/gradient-ascent import-sync-manifest /path/to/manifest.json
.codex/bin/gradient-ascent build-plan /path/to/training-plan.xlsx
```

For Apple Health and Garmin, the same path can be saved in the Connections view. A later dashboard refresh will re-import the current contents of that local path.

An optional, separately installed companion can supply activity or recovery data through a versioned local JSON sync manifest. Import that manifest with `import-sync-manifest`, then run `refresh`. Authentication and provider network access for that companion remain in the separate application. Gradient Ascent never receives provider credentials. Do not enter passwords, API keys, session cookies, MFA codes, or OAuth tokens; do not print manifests or their raw activity and recovery payloads into model context.

### Optional Ride with GPS connection

Use the Training Center's **Connections** view: choose **Install and connect**, or
**Connect** to use an existing supported official CLI. Click the displayed sign-in
link yourself, choosing the correct browser profile. You can also run:

```bash
.codex/bin/gradient-ascent ride setup --install
.codex/bin/gradient-ascent ride status
.codex/bin/gradient-ascent ride check
.codex/bin/gradient-ascent ride sync --days 14
```

`--install` explicitly authorizes a download of the checksum-pinned official
[`ride` v0.1.0 binary](https://github.com/ridewithgps/ride-cli/releases/tag/v0.1.0).
The supported release provides macOS arm64/x64 and Linux x64 binaries. Without that
flag, setup uses an already installed, verified binary; select one with
`ride setup --executable /absolute/path/to/ride`. `ride setup --days N` changes the
recent-sync lookback. No Claude installation is required for the vendor's login or
API commands. Its API command may also perform the vendor's periodic GitHub release
check. See the [official CLI documentation](https://github.com/ridewithgps/ride-cli/tree/v0.1.0).

Gradient Ascent invokes the real vendor CLI against `https://ridewithgps.com`; the
CLI handles OAuth and stores its own tokens. By default, the vendor's existing
configuration stays in use. If you already maintain a separate private vendor
configuration directory, select it with `ride setup --config-dir /absolute/path`.
Gradient Ascent stores that non-secret path, not its contents. Never copy API keys,
passwords, cookies, or tokens into Gradient Ascent, a prompt, or the public checkout.
This is an independent integration, not a claim of vendor endorsement.

`ride status` is offline. **Check** / `ride check` explicitly verifies the current
vendor session. To correct a login, run `ride setup --reauth` and click its new link;
the workspace refuses to mix a different account into existing athlete data. Use a
separate private athlete workspace for a different rider.

For older activity history:

```bash
.codex/bin/gradient-ascent ride sync --history
```

The scan is bounded; repeat it, or choose **Import older rides** again, until the
reported history progress is complete. It resumes automatically. Use
`ride sync --history --restart-history` only when you intend to restart that scan. Unchanged
trips are skipped; changed trips replace their earlier imported version. Available
sensor samples, timestamps, and laps are retained in private local recordings.

Use **Stop syncing** or `ride disable` to stop automatic Ride with GPS requests.
This preserves imported history and does not log out the vendor CLI or revoke its
tokens. Strava archives, Garmin exports, and optional companion manifests continue
to work independently; no unofficial live Strava or Garmin connector is enabled.

## 6. Build and serve the Training Center

```bash
.codex/bin/gradient-ascent refresh
.codex/bin/gradient-ascent serve-training-center --port 8787
```

The server binds only to `127.0.0.1`. If port 8787 is occupied it tries the next ten ports unless `--strict-port` is set. Writes require a per-process random token, a loopback Host header, and a matching same-origin request.

Verify that the printed `/api/health` endpoint returns the workspace ID shown at launch.

## Optional remote or devbox setup

Use synthetic data for a clean-room remote test. Copying a personal Strava, Garmin, Apple Health, FIT, TCX, or GPX export to a devbox places that private data on the remote host; prefer a local installation for real athlete data.

Paths such as `~/Downloads` refer to the remote host when Gradient Ascent runs there. Upload any synthetic fixture before importing it, for example:

```bash
devbox upload <devbox-name> ./sample-ride.fit /tmp/gradient-ascent-inputs/sample-ride.fit --mkdir
```

Run the Training Center on a fixed remote port:

```bash
.codex/bin/gradient-ascent serve-training-center --port 8787 --strict-port
```

From the laptop, forward that same port and open the printed `http://127.0.0.1:8787/...` URL locally:

```bash
devbox port-forward <devbox-name> --ports 8787:8787
```

The forwarded browser origin remains loopback. Verify `/api/health` through the forwarded URL before testing notes, refresh, or uploads.

## Troubleshooting

### `gradient-ascent` is not on PATH

Use the checkout launcher before workspace initialization:

```bash
/path/to/gradient-ascent/.venv/bin/gradient-ascent --help
```

After initialization, use:

```bash
.codex/bin/gradient-ascent --help
```

### The Training Center shows stale data

Use the refresh button or run:

```bash
.codex/bin/gradient-ascent refresh
```

Check `derived/post_sync_summary.json` for source coverage and local import errors.
If Ride with GPS is enabled, normal refresh fetches its recent changes before one
local rebuild. Use `refresh --local-only` when you want an offline rebuild, and
`ride check` when you explicitly want to test the vendor session. A provider failure
must be reported as a failure; a local rebuild alone is not proof that new rides were
fetched.

### A local export cannot be found

Run `connections-test` for the configured source, or import the path directly to see the exact missing directory or file:

```bash
.codex/bin/gradient-ascent connections-test apple_health
.codex/bin/gradient-ascent connections-test garmin
```

### Remove a workspace

Preview first, then repeat the exact resolved path as confirmation:

```bash
.codex/bin/gradient-ascent purge-workspace ~/code/gradient-ascent-workspace
.codex/bin/gradient-ascent purge-workspace ~/code/gradient-ascent-workspace --confirm /absolute/path/from/preview
```

This permanently deletes the complete workspace, including any Git history the rider created there.
