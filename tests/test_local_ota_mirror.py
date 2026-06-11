import hashlib
import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "local_ota_mirror.py"
SNAPSHOT_KIND = "velatermOtaSnapshot"
TIMESTAMP_KIND = "velatermOtaTimestamp"
ASSET_SUFFIX = ".velaterm-ota"


def load_module():
    spec = importlib.util.spec_from_file_location("local_ota_mirror", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def write_snapshot_remote(remote, snapshot_id="20260605T000000Z-vtest", asset_suffix=ASSET_SUFFIX):
    snapshot_remote_dir = remote / "snapshots" / snapshot_id
    snapshot_remote_dir.mkdir(parents=True, exist_ok=True)

    asset_bytes = b"velaterm ota package"
    asset_sha = hashlib.sha256(asset_bytes).hexdigest()
    asset_name = f"sha256-{asset_sha[:12]}-VelaTerm-main-darwin-aarch64-0.1.1{asset_suffix}"
    asset_path = remote / asset_name
    asset_path.write_bytes(asset_bytes)

    snapshot = {
        "schemaVersion": 1,
        "kind": SNAPSHOT_KIND,
        "snapshotId": snapshot_id,
        "channel": "stable",
        "version": 2,
        "publishedAt": "2026-06-05T00:00:00.000Z",
        "minimumHostVersion": "0.1.0",
        "metadata": [],
        "targets": [
            {
                "targetId": "app:darwin-aarch64:0.1.1",
                "kind": "app",
                "platform": "darwin-aarch64",
                "version": "0.1.1",
                "assetName": asset_name,
                "sha256": asset_sha,
                "payloadSha256": hashlib.sha256(b"raw payload").hexdigest(),
                "length": len(asset_bytes),
                "tauriSignature": "tauri-signature",
                "encryption": {
                    "format": "dnota1",
                    "payloadPath": "payloads/VelaTerm.app.tar.gz",
                    "keyId": "main-app-ota-v1",
                },
                "locations": [
                    {"kind": "githubRelease", "url": asset_path.resolve().as_uri()},
                    {"kind": "snapshotMirror", "path": f"assets/{asset_name}"},
                ],
            },
        ],
        "signature": "snapshot-signature",
    }
    snapshot_text = json.dumps(snapshot, separators=(",", ":"))
    snapshot_bytes = snapshot_text.encode("utf-8")
    (snapshot_remote_dir / "snapshot.json").write_text(snapshot_text, encoding="utf-8")

    timestamp = {
        "schemaVersion": 1,
        "kind": TIMESTAMP_KIND,
        "channel": "stable",
        "version": 2,
        "snapshotId": snapshot_id,
        "snapshotPath": f"snapshots/{snapshot_id}/snapshot.json",
        "snapshotSha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "snapshotLength": len(snapshot_bytes),
        "publishedAt": "2026-06-05T00:00:00.000Z",
        "expiresAt": "2026-07-05T00:00:00.000Z",
        "signature": "timestamp-signature",
    }
    write_json(remote / "timestamp.json", timestamp)
    return {"snapshot_id": snapshot_id, "asset_name": asset_name, "asset_bytes": asset_bytes}


class LocalOtaMirrorTests(unittest.TestCase):
    def test_metadata_verifier_allows_timestamp_site_context_without_resigning(self):
        mirror = load_module()
        timestamp = {
            "schemaVersion": 1,
            "kind": TIMESTAMP_KIND,
            "channel": "stable",
            "version": 2,
            "snapshotId": "vtest",
            "snapshotPath": "snapshots/vtest/snapshot.json",
            "snapshotSha256": "a" * 64,
            "snapshotLength": 128,
            "publishedAt": "2026-06-05T00:00:00.000Z",
            "expiresAt": "2026-07-05T00:00:00.000Z",
            "signature": (
                "cb9396ecf7d26dc1a1ad7485491c983472ad3a752d6f66409afed9ecd9ccba67"
                "46af2307e302b3f88ee70e4e7e00c60b171900d7d74e3afaff02a669505c6f00"
            ),
            "site": {"kind": "github"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            public_key = pathlib.Path(temp_dir) / "metadata-public-key.hex"
            public_key.write_text(
                "ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22c",
                encoding="utf-8",
            )
            verifier = mirror.build_metadata_verifier(public_key)
            text = json.dumps(timestamp, separators=(",", ":"))

            verifier("timestamp.json", text, timestamp["signature"])

            with self.assertRaisesRegex(RuntimeError, "failed to verify metadata"):
                verifier("snapshots/vtest/snapshot.json", text, timestamp["signature"])

    def test_config_file_supplies_defaults_and_cli_overrides(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            config_path = root / "mirror-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "port": 19090,
                        "interval": 42,
                        "timeout": 9,
                        "advertise_host": "192.168.1.20",
                        "github_proxy": "http://127.0.0.1:7890",
                        "metadata_public_key_file": "/tmp/metadata-public-key.hex",
                    },
                ),
                encoding="utf-8",
            )

            parser = mirror.build_parser()
            args = parser.parse_args(["run", "--config", str(config_path)])
            mirror.apply_config(args)

            self.assertEqual(args.port, 19090)
            self.assertEqual(args.interval, 42)
            self.assertEqual(args.timeout, 9)
            self.assertEqual(args.github_proxy, "http://127.0.0.1:7890")
            self.assertEqual(args.metadata_public_key_file, "/tmp/metadata-public-key.hex")
            self.assertEqual(mirror.public_base_url(args), "http://192.168.1.20:19090")

            args = parser.parse_args(["run", "--config", str(config_path), "--interval", "7"])
            mirror.apply_config(args)

            self.assertEqual(args.interval, 7)

    def test_sync_requires_timestamp_metadata(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()

            with self.assertRaisesRegex(RuntimeError, "timestamp metadata"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=root / "cache",
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                    metadata_verifier=lambda name, text, signature: None,
                )

    def test_sync_downloads_velaterm_snapshot_assets_atomically(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote)
            cache = root / "cache"

            result = mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache,
                public_base_url="http://192.168.1.20:18080",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )

            current = json.loads((cache / "current.json").read_text(encoding="utf-8"))
            asset = cache / "snapshots" / fixture["snapshot_id"] / "assets" / fixture["asset_name"]

            self.assertEqual(result.asset_count, 1)
            self.assertEqual(current["snapshot"], fixture["snapshot_id"])
            self.assertEqual(asset.read_bytes(), fixture["asset_bytes"])

    def test_sync_rejects_non_velaterm_ota_assets(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            write_snapshot_remote(remote, asset_suffix=".zip")

            with self.assertRaisesRegex(ValueError, "assetName is invalid"):
                mirror.sync_mirror(
                    remote_base_url=remote.resolve().as_uri(),
                    cache_dir=root / "cache",
                    public_base_url="http://192.168.1.20:18080",
                    timeout=2,
                    metadata_verifier=lambda name, text, signature: None,
                )

    def test_server_marks_root_timestamp_as_mirror_without_resigning(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            fixture = write_snapshot_remote(remote)
            cache = root / "cache"
            mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache,
                public_base_url="http://127.0.0.1:0",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )
            cached_timestamp = json.loads(
                (
                    cache / "snapshots" / fixture["snapshot_id"] / "timestamp.json"
                ).read_text(encoding="utf-8")
            )

            server = mirror.build_server(bind="127.0.0.1", port=0, cache_dir=cache)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with urllib.request.urlopen(f"http://{host}:{port}/timestamp.json", timeout=2) as response:
                    served_timestamp = json.loads(response.read().decode("utf-8"))

                expected = dict(cached_timestamp)
                expected["site"] = {"kind": "mirror"}
                self.assertEqual(served_timestamp, expected)
                self.assertEqual(served_timestamp["signature"], cached_timestamp["signature"])
            finally:
                server.shutdown()
                server.server_close()

    def test_server_returns_404_for_legacy_endpoints(self):
        mirror = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            remote = root / "remote"
            remote.mkdir()
            write_snapshot_remote(remote)
            cache = root / "cache"
            mirror.sync_mirror(
                remote_base_url=remote.resolve().as_uri(),
                cache_dir=cache,
                public_base_url="http://127.0.0.1:0",
                timeout=2,
                metadata_verifier=lambda name, text, signature: None,
            )

            server = mirror.build_server(bind="127.0.0.1", port=0, cache_dir=cache)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                for path in ("/latest.json", "/manifest.json"):
                    with self.assertRaises(urllib.error.HTTPError) as context:
                        urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=2)
                    self.assertEqual(context.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
