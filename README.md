# VelaTerm OTA Site

This repository is the public static OTA metadata site for VelaTerm.

GitHub Pages publishes lightweight metadata from the `main` branch repository root:

- `timestamp.json`
- `snapshots/<snapshotId>/snapshot.json`
- `index.html`
- `README.md`

Versioned `.velaterm-ota` packages are published as GitHub Release assets. Do not commit updater payloads, private OTA packages, signing keys, decryption keys, CI secrets, or release records that contain local machine paths.

GitHub Pages endpoint:

```toml
[updater]
ota_timestamp_url = "https://acer-0606.github.io/velaterm-ota-site/timestamp.json"
```

The client uses `timestamp.json -> snapshot.json -> targets`. This site does not provide `latest.json` or `manifest.json` compatibility endpoints.

## Metadata

VelaTerm OTA metadata is app-only:

- timestamp kind: `velatermOtaTimestamp`
- snapshot kind: `velatermOtaSnapshot`
- target kind: `app`
- artifact extension: `.velaterm-ota`
- target id: `app:<platform>:<version>`
- replacement key: `app:<platform>`

Supported platforms:

- `darwin-aarch64`
- `windows-x86_64`
- `linux-x86_64`

## Publishing

From the VelaTerm repository:

```bash
node scripts/publish-ota-snapshot.mjs \
  --release-record artifacts/release-record-<version>/<platform>.json \
  --site-dir subrepos/ota-site \
  --tag v<version> \
  --repo acer-0606/velaterm \
  --publish
```

Commit and push the updated `timestamp.json` and immutable `snapshots/<snapshotId>/snapshot.json` after uploading the referenced `.velaterm-ota` files to GitHub Releases.

## Local LAN Mirror

Start a polling LAN mirror:

```bash
python3 tools/local_ota_mirror.py run --port 18080 --interval 300
```

From the parent VelaTerm repository:

```bash
python3 subrepos/ota-site/tools/local_ota_mirror.py run --port 18080 --interval 300
```

The mirror serves:

- `http://<lan-ip>:18080/timestamp.json`
- `http://<lan-ip>:18080/snapshots/<snapshot-id>/snapshot.json`
- `http://<lan-ip>:18080/snapshots/<snapshot-id>/assets/<name>.velaterm-ota`

All cached files live under `.local-ota/`, which is ignored by git.

## Mirror Configuration

Command-line flags override `.local-ota/config.json`.

```json
{
  "port": 18080,
  "bind": "0.0.0.0",
  "interval": 300,
  "timeout": 30,
  "remote_base_url": "https://acer-0606.github.io/velaterm-ota-site",
  "advertise_host": "192.168.1.20",
  "github_proxy": "http://127.0.0.1:7890",
  "metadata_public_key_file": "/path/to/metadata-public-key.hex"
}
```
