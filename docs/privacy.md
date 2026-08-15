# Privacy and security

Gradient Ascent is designed around a local athlete workspace.

## Data boundary

The plugin checkout contains application code and skills. Athlete data belongs in a separate workspace selected during setup. The application refuses to use a directory inside the plugin checkout as its athlete workspace.

Raw and derived athlete files remain on the local machine unless the rider independently chooses to move or publish them. Gradient Ascent does not upload source files to a service.

The generated workspace `.gitignore` excludes:

- `.env`
- raw Strava details, laps, and streams
- standalone activity recordings and their parsed streams and laps
- official Strava archive drops
- official Garmin Connect exports and normalized daily recovery files
- Apple Health exports and normalized health files
- provider-neutral companion sync manifests under `integrations/`
- logs
- private workspace metadata under `.codex/cache/`
- the writable incremental Training Center cache under `derived/.cache/`

Generated summaries and dashboards can also contain sensitive training information. Keep the entire workspace private unless it has been deliberately reviewed and sanitized.

## Credentials

Gradient Ascent does not implement provider login or OAuth. It does not request, read, validate, or persist provider passwords, API keys, client secrets, refresh tokens, session cookies, or MFA codes.

Supported source paths consume local files supplied by the athlete:

- official Strava account archive
- standalone FIT, TCX, or GPX activity recording
- official Garmin Connect account export
- Apple Health export
- training-plan CSV or XLSX
- a versioned, provider-neutral local companion sync manifest

The connection registry stores only non-secret local export paths in `connections/config.json`. Optional companion applications are separate projects: any provider authentication or network access remains entirely outside Gradient Ascent. Companion manifests contain only explicitly allowed, compact activity and recovery fields; credential-like keys, unknown fields, symbolic links, and provider impersonation are rejected. Imported manifests are kept in the private, Git-ignored `integrations/` directory.

## Local server

The Training Center binds to `127.0.0.1` only. It has no remote-bind option.

All requests require a loopback Host header. Write endpoints additionally require:

- a matching same-origin Origin or Referer when present
- a random write token generated in memory when the server starts

The token is not written to disk. A server restart invalidates it.

Workspace writes are serialized with an opaque lock file in a user-private namespace inside the operating system's temporary directory. Its filename is a one-way hash of filesystem identifiers; it contains no athlete data or workspace path and may remain after a workspace is deleted.

## Model context

Skills may use compact normalized activity records and derived aggregates. They should not load raw source archives, sample-by-sample GPS or sensor traces, lap-detail payloads, raw health exports, unredacted companion manifests, or credential files into model context, and should never pass those raw files to subagents. When a raw file is needed for local parsing, pass its filesystem path to the CLI.

## Deletion

`gradient-ascent purge-workspace` requires an explicit path and a second exact-path confirmation. It removes the complete athlete workspace. Preview the operation before confirming it.
