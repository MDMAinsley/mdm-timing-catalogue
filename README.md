# MDM Timing Catalogue

Tools for identifying episode cuts from audio and distributing intro and outro timings to MDM Flix. A playback snapshot indexes the show, season and episode, then uses the measured runtime and two position-bound audio digests to identify the cut.

The initial snapshot is empty. Private playback data and account credentials belong outside this repository.

## Review and export

A maintainer can compile a private schema 1 ledger into a public schema 2 snapshot.

```sh
python export_catalogue.py private-ledger.json playback-snapshot.json
python -m unittest discover
python validate_snapshot.py playback-snapshot.json
```

The ledger contains cuts keyed by an internal ID, each with an episode triple, runtime in seconds, audio anchors and timing proposals. It also contains reviews and a trusted-account allowlist. Each review applies to a single proposal revision. The ledger must come from an authenticated review process.

To generate the manual review queue, run `python review_queue.py private-ledger.json review-queue.json`. A current marker revision enters the queue after ten independent Bad votes. An account's latest vote replaces its earlier vote on that revision. Replaced and withdrawn revisions do not create current review tasks. Queue export contains vote counts and timing details, with individual voter identities omitted. Creating GitHub review issues and accepting public submissions remain separate integration work.

Trusted reviewer approval takes precedence over community approval. Community approval requires more than 75 percent positive votes and at least five accounts. A software estimate can be exported as provisional when eligible. Intro and outro are selected independently. Playback snapshots contain digests, timings and review status. Keep individual votes, account IDs, playback URLs, torrent identifiers and raw media private.

## Windows companion

With Flix 0.15.3 or later, the MDM audio diagnostic bridge and the local FFmpeg and Chromaprint tools installed, run:

```powershell
python watch_hints.py --public-catalogue --fpcalc PATH_TO_FPCALC --output CACHE_DIRECTORY
```

The companion checks for a new snapshot on playback requests. It refreshes a valid cache after a day, keeps the last valid copy during an outage, and skips audio scans for episodes with no catalogue entry. Private test snapshots can still be selected with `--catalogue PATH`.

Authenticated submissions and Kodi ratings are planned. The catalogue includes no contributions yet.

## Anonymous and signed-in feedback

The vote-summary helper keeps anonymous-key votes and verified signed-in votes in separate totals and percentages. Each contributor's latest vote replaces the earlier one for a marker revision. Linked anonymous keys are excluded from guest totals to avoid counting both identities. These helpers consume records validated by the submission service; accepting a client-supplied account ID or trust flag would bypass that validation.

The submission service, anonymous-key issuance, abuse screening and sign-in integration are still pending. Anonymous promotion thresholds and weighting have not been enabled.

`vote_store.py` provides private SQLite storage for the service. A contributor has one current vote per identified cut, marker and timing record revision. Provider URL changes do not create new targets. Guest-to-account linking preserves an existing signed-in choice and invalidates the linked guest identity for future votes. All actor registration and linking methods are internal and require identity verification by the future HTTP service.

The selection/export helpers accept anonymous reviews only when the service explicitly identifies their keys as eligible. The provisional quorum is twenty eligible anonymous voters versus five verified signed-in voters, with more than 75 percent positive required. Signed-in approval wins when both qualify. Eligibility screening and live submission are not enabled yet.
# Contribution inbox deployment

`submission_api.py` supplies a WSGI application factory, `submission_api:create_app()`, backed by private SQLite storage selected with `MDM_SKIP_DATA`. Run behind a production WSGI server and TLS reverse proxy. Never expose it without connection-level signup throttling, request size limits and restricted database permissions.

The API uses `POST /v1/contributors` with `{"consent":true}` to issue an anonymous bearer key. Store the returned key privately. `POST /v1/proposals` accepts only episode, duration, two position-bound audio anchors, marker, start, end and origin. A receipt means the proposal reached the pending inbox. It does not mean it was verified or published. `POST /v1/votes` accepts a known server target and boolean good. Clients cannot create rating targets, assert reviewer status, or promote records. GitHub sign-in and automatic publication are not implemented.

Raw keys are not retained. Anonymous key hashes, ballots and pending payloads are private data and must never be included in public exports. The server should not log request bodies or authorization headers. Infrastructure can still see connection metadata. Tests use only synthetic data.

