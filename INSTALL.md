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

An optional, separately installed companion can supply activity or recovery data through a versioned local JSON sync manifest. Import that manifest with `import-sync-manifest`, then run `refresh`. Any authentication or provider network access belongs exclusively to the companion. Gradient Ascent has no provider-login setup and never receives provider credentials. Do not enter passwords, API keys, session cookies, MFA codes, or OAuth tokens; do not print manifests or their raw activity and recovery payloads into model context.

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
