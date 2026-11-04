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

Consented anonymous submissions and Kodi ratings reach the private service. The public catalogue has no approved timing cuts yet; received claims are not automatically approved.

## Anonymous and signed-in feedback

The vote-summary helper keeps anonymous-key votes and verified signed-in votes in separate totals and percentages. Each contributor's latest vote replaces the earlier one for a marker revision. Linked anonymous keys are excluded from guest totals to avoid counting both identities. These helpers consume records validated by the submission service; accepting a client-supplied account ID or trust flag would bypass that validation.

The submission service and anonymous-key issuance are live. Automatic abuse/eligibility screening and GitHub device sign-in remain unfinished. New keys are not automatically eligible for promotion.

`vote_store.py` provides private SQLite storage for the service. A contributor has one current vote per identified cut, marker and timing record revision. Provider URL changes do not create new targets. Guest-to-account linking preserves an existing signed-in choice and invalidates the linked guest identity for future votes. All actor registration and linking methods are internal and require identity verification by the future HTTP service.

The selection/export helpers accept anonymous reviews only when the service explicitly identifies their keys as eligible. The provisional quorum is twenty eligible anonymous voters versus five verified signed-in voters, with more than 75 percent positive required. Signed-in approval wins when both qualify. Eligibility must be granted through authenticated server administration. Automatic eligibility screening and TV sign-in are not enabled yet.
## Contribution inbox deployment

`submission_api.py` supplies a WSGI application factory, `submission_api:create_app()`, backed by private SQLite storage selected with `MDM_SKIP_DATA`. Run behind a production WSGI server and TLS reverse proxy. Never expose it without connection-level signup throttling, request size limits and restricted database permissions.

Clients may send an empty anchor list when no cut fingerprint is available. These entries receive `pending_cut_verification` and cannot establish cross-stream identity. Optional `assessment` (good, bad, correction) and a marker/revision reference preserve feedback for review. With a unique operation ID, a Good/Bad assessment can attach to an administrator-verified cut and exact current timing revision. Other receipts remain pending. Receipt acknowledgement is never public approval.

The API uses `POST /v1/contributors` with `{"consent":true}` to issue an anonymous bearer key. Store the returned key privately. `POST /v1/proposals` accepts only episode, duration, two position-bound audio anchors, marker, start, end and origin. A receipt means the proposal reached the pending inbox. It does not mean it was verified or published. `POST /v1/votes` accepts a known server target and boolean good. Clients cannot create rating targets, assert reviewer status, or promote records. GitHub sign-in is not implemented. Validated approved snapshots can be published by the isolated VPS publisher.

Raw keys are not retained. Anonymous key hashes, ballots and pending payloads are private data and must never be included in public exports. The server should not log request bodies or authorization headers. Infrastructure can still see connection metadata. Tests use only synthetic data.


## Administrator review and publication

`review_registry.py` keeps verified cuts, immutable marker revisions, explicit contributor eligibility, trusted reviewer roles and an audit trail in the private vote database. These administration methods are not exposed as HTTP routes. Admission requires an explicit human confirmation that the two audio anchors belong to the stated cut. A client reference, runtime alone, or a received correction cannot grant that confirmation. Admission also does not approve a manually marked timing.

A canonical cut/intro-or-outro has one marker history. Changing its boundaries creates a new revision with fresh votes. Withdrawn/superseded timings are not valid vote targets. Reused operation IDs cannot change payloads, repeated operations cannot add ballots, and replaying an older receipt cannot replace a newer assessment. Legacy clients without operation IDs stay in the review inbox.

`review_workflow.py --data PRIVATE_DIRECTORY report` shows the private moderation queue. `admit RECEIPT --confirmed-cut --operator LABEL` admits cut evidence and a marker candidate. Add `--software` only when deliberately accepting a software candidate. `policy ACTOR --eligible --operator LABEL` changes eligibility; `--trusted` requires a verified signed-in identity registered internally. `review TARGET REVIEWER --good --operator LABEL` records an authenticated administrator's trusted approval; omitting `--good` records a veto. `withdraw TARGET --operator LABEL` removes the current revision from future exports. Do not treat an arbitrary HTTP account ID as a verified identity. TV GitHub sign-in and automated owner-correction attribution still need integration.

`export DESTINATION` creates an immutable bundle containing only playback-snapshot.json, review-queue.json and catalogue-release.json. The review queue gets one entry at ten eligible Bad votes. Eligible anonymous and verified signed-in totals stay separate; correction clusters exclude distant outliers and are suggestions for review, not silent replacements for trusted timings. Anonymous promotion needs twenty eligible votes, signed-in promotion five, and both require strictly more than 75 percent positive.

`export_worker.py` writes private moderation details separately and rotates up to twenty public-only release directories. `publish_catalogue.py` validates the bundle again, stages only the three public files, verifies Git preserves their bytes and pushes an ordinary forward commit. The VPS uses a repository-specific write deploy key, not an addon-embedded credential. Repeating an unchanged export makes no new commit. Dirty workspaces, divergent history, invalid fields and changed hashes stop publication without discarding commits.
