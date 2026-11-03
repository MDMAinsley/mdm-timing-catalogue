# MDM Timing Catalogue

Tools for identifying episode cuts from audio and distributing intro and outro timings to MDM Flix. A playback snapshot indexes the show, season and episode, then uses the measured runtime and two position-bound audio digests to identify the cut.

The initial snapshot is empty. Private playback data and account credentials belong outside this repository.

## Review and export

A maintainer can compile a private schema 1 ledger into a public schema 2 snapshot.

```sh
python export_catalogue.py private-ledger.json playback-snapshot.json
python -m unittest discover
```

The ledger contains cuts keyed by an internal ID, each with an episode triple, runtime in seconds, audio anchors and timing proposals. It also contains reviews and a trusted-account allowlist. Each review applies to a single proposal revision. The ledger must come from an authenticated review process.

Trusted reviewer approval takes precedence over community approval. Community approval requires more than 75 percent positive votes and at least five accounts. A software estimate can be exported as provisional when eligible. Intro and outro are selected independently. Playback snapshots contain digests, timings and review status. Keep individual votes, account IDs, playback URLs, torrent identifiers and raw media private.

## Windows companion

With Flix 0.15.3 or later, the MDM audio diagnostic bridge and the local FFmpeg and Chromaprint tools installed, run:

```powershell
python watch_hints.py --public-catalogue --fpcalc PATH_TO_FPCALC --output CACHE_DIRECTORY
```

The companion checks for a new snapshot on playback requests. It refreshes a valid cache after a day, keeps the last valid copy during an outage, and skips audio scans for episodes with no catalogue entry. Private test snapshots can still be selected with `--catalogue PATH`.

Authenticated submissions and Kodi ratings are planned. The catalogue includes no contributions yet.
