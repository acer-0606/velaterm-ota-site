#!/usr/bin/env python3
"""Local LAN mirror for VelaTerm OTA metadata and .velaterm-ota packages."""

import argparse
import base64
import binascii
import hashlib
import http.server
import io
import json
import os
import posixpath
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


SITE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = SITE_ROOT / ".local-ota"
DEFAULT_CONFIG_PATH = DEFAULT_CACHE_DIR / "config.json"
DEFAULT_REMOTE_BASE_URL = "https://acer-0606.github.io/velaterm-ota-site"
OPTIONAL_ROOT_METADATA_FILES = ()
ROOT_CURRENT_METADATA_FILES = ("timestamp.json",)
REQUIRED_SNAPSHOT_METADATA_FILES = ("timestamp.json", "snapshot.json")
USER_AGENT = "VelaTermLocalOtaMirror/1.0"
GITHUB_PROXY_DOMAINS = ("github.com", "github.io", "githubusercontent.com")
ED25519_P = 2**255 - 19
ED25519_Q = 2**252 + 27742317777372353535851937790883648493
ED25519_D = -121665 * pow(121666, ED25519_P - 2, ED25519_P) % ED25519_P
ED25519_I = pow(2, (ED25519_P - 1) // 4, ED25519_P)
ED25519_BASE_Y = 4 * pow(5, ED25519_P - 2, ED25519_P) % ED25519_P


class DownloadRef:
    def __init__(self, url, sha256, asset_name, length=None):
        self.url = url
        self.sha256 = sha256
        self.asset_name = asset_name
        self.length = length


class SyncResult:
    def __init__(self, metadata_count, asset_count, download_count, reused_count, snapshot_name, changed):
        self.metadata_count = metadata_count
        self.asset_count = asset_count
        self.download_count = download_count
        self.reused_count = reused_count
        self.snapshot_name = snapshot_name
        self.changed = changed


def sync_mirror(remote_base_url, cache_dir, public_base_url, timeout=30, github_proxy="", metadata_verifier=None):
    """Fetch remote metadata/assets and write a self-contained local mirror."""
    cache_dir = Path(cache_dir)
    timestamp = fetch_v2_timestamp(
        remote_base_url,
        timeout,
        github_proxy=github_proxy,
        metadata_verifier=metadata_verifier,
    )
    if timestamp is not None:
        return sync_v2_mirror(
            remote_base_url,
            cache_dir,
            public_base_url,
            timestamp,
            timeout,
            github_proxy=github_proxy,
            metadata_verifier=metadata_verifier,
        )


def sync_v2_mirror(remote_base_url, cache_dir, public_base_url, timestamp, timeout=30, github_proxy="", metadata_verifier=None):
    cache_dir = Path(cache_dir)
    snapshots_dir = cache_dir / "snapshots"
    snapshot_name = timestamp["value"].get("snapshotId")
    if not valid_snapshot_name(snapshot_name):
        raise ValueError("invalid v2 snapshotId: %r" % (snapshot_name,))

    snapshot = fetch_v2_snapshot(
        remote_base_url,
        timestamp["value"],
        timeout,
        github_proxy=github_proxy,
        metadata_verifier=metadata_verifier,
    )
    if snapshot["value"].get("snapshotId") != snapshot_name:
        raise ValueError("snapshotId mismatch between timestamp and snapshot")

    optional_metadata = fetch_optional_root_metadata(remote_base_url, timeout, github_proxy=github_proxy)
    refs_by_url = collect_v2_download_refs(snapshot["value"])
    for ref in collect_optional_root_download_refs(optional_metadata).values():
        add_download_ref(refs_by_url, ref)
    unique_refs = unique_refs_by_asset(refs_by_url)
    public_base_url = public_base_url.rstrip("/")

    snapshot_dir = snapshots_dir / snapshot_name
    staging_dir = snapshots_dir / (snapshot_name + ".tmp")
    assets_dir = staging_dir / "assets"
    download_count = 0
    reused_count = 0

    state_metadata = {
        "timestamp.json": timestamp["value"],
        "snapshot.json": snapshot["value"],
        **optional_metadata,
    }

    if snapshot_dir.exists():
        if not v2_snapshot_metadata_matches(snapshot_dir, state_metadata):
            raise ValueError("existing v2 snapshot %s does not match fetched metadata/assets" % snapshot_name)
        repaired = repair_v2_snapshot_assets(
            snapshot_dir,
            unique_refs,
            timeout,
            github_proxy=github_proxy,
        )
        write_v2_current_state(
            cache_dir,
            remote_base_url,
            public_base_url,
            state_metadata,
            unique_refs,
            snapshot_name,
        )
        return SyncResult(
            metadata_count=len(state_metadata),
            asset_count=len(unique_refs),
            download_count=repaired["downloaded"],
            reused_count=repaired["reused"],
            snapshot_name=snapshot_name,
            changed=repaired["downloaded"] > 0,
        )

    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
        for ref in unique_refs:
            if copy_existing_asset(cache_dir, ref, assets_dir / ref.asset_name):
                reused_count += 1
            elif ensure_asset(ref, assets_dir, timeout, github_proxy=github_proxy):
                download_count += 1
            else:
                reused_count += 1

        write_json_atomic(staging_dir / "timestamp.json", timestamp["value"])
        write_json_atomic(staging_dir / "snapshot.json", snapshot["value"])
        for name, document in optional_metadata.items():
            write_json_atomic(staging_dir / name, document)

        os.replace(str(staging_dir), str(snapshot_dir))
        write_v2_current_state(
            cache_dir,
            remote_base_url,
            public_base_url,
            state_metadata,
            unique_refs,
            snapshot_name,
        )
        return SyncResult(
            metadata_count=len(state_metadata),
            asset_count=len(unique_refs),
            download_count=download_count,
            reused_count=reused_count,
            snapshot_name=snapshot_name,
            changed=True,
        )
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise


def fetch_v2_timestamp(remote_base_url, timeout, github_proxy="", metadata_verifier=None):
    remote_base_url = remote_base_url.rstrip("/") + "/"
    url = urllib.parse.urljoin(remote_base_url, "timestamp.json")
    try:
        text = fetch_bytes(url, timeout, github_proxy=github_proxy).decode("utf-8")
    except Exception as error:
        raise RuntimeError("failed to fetch timestamp metadata %s: %s" % (url, error))

    try:
        value = json.loads(text)
    except Exception as error:
        raise RuntimeError("failed to parse timestamp metadata %s: %s" % (url, error))
    if not isinstance(value, dict):
        raise ValueError("timestamp metadata must be a JSON object")
    if value.get("kind") != "velatermOtaTimestamp":
        raise ValueError("timestamp kind must be velatermOtaTimestamp")
    verify_signed_metadata("timestamp.json", text, value.get("signature"), metadata_verifier)
    return {"name": "timestamp.json", "text": text, "value": value}


def fetch_v2_snapshot(remote_base_url, timestamp, timeout, github_proxy="", metadata_verifier=None):
    snapshot_path = timestamp.get("snapshotPath")
    if not valid_relative_metadata_path(snapshot_path):
        raise ValueError("timestamp snapshotPath is invalid")
    expected_sha256 = timestamp.get("snapshotSha256")
    if not is_sha256(expected_sha256):
        raise ValueError("timestamp snapshotSha256 is invalid")
    expected_length = timestamp.get("snapshotLength")
    if not valid_positive_int(expected_length):
        raise ValueError("timestamp snapshotLength is invalid")

    remote_base_url = remote_base_url.rstrip("/") + "/"
    url = urllib.parse.urljoin(remote_base_url, snapshot_path)
    data = fetch_bytes(url, timeout, github_proxy=github_proxy)
    if len(data) != expected_length:
        raise ValueError(
            "length mismatch for %s: expected %s, got %s"
            % (snapshot_path, expected_length, len(data)),
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise ValueError(
            "sha256 mismatch for %s: expected %s, got %s"
            % (snapshot_path, expected_sha256, digest),
        )
    text = data.decode("utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("v2 snapshot metadata must be a JSON object")
    if value.get("kind") != "velatermOtaSnapshot":
        raise ValueError("snapshot kind must be velatermOtaSnapshot")
    verify_signed_metadata(snapshot_path, text, value.get("signature"), metadata_verifier)
    return {"name": snapshot_path, "text": text, "value": value}


def verify_signed_metadata(name, text, signature, metadata_verifier):
    if not callable(metadata_verifier):
        raise RuntimeError("metadata verifier is required for v2 OTA metadata: %s" % name)
    if not isinstance(signature, str) or not signature:
        raise ValueError("metadata signature is required: %s" % name)
    metadata_verifier(name, text, signature)


def collect_v2_download_refs(snapshot):
    refs_by_url = {}
    refs_by_name = {}
    targets = snapshot.get("targets")
    if not isinstance(targets, list):
        raise ValueError("snapshot targets must be an array")

    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("snapshot target must be a JSON object")
        asset_name = target.get("assetName")
        if not valid_asset_name(asset_name):
            raise ValueError("snapshot target assetName is invalid")
        sha256 = target.get("sha256")
        if not is_sha256(sha256):
            raise ValueError("snapshot target %s has invalid sha256" % asset_name)
        length = target.get("length")
        if not valid_positive_int(length):
            raise ValueError("snapshot target %s has invalid length" % asset_name)
        url = first_target_url(target)
        if not url:
            raise ValueError("snapshot target %s does not provide a download URL" % asset_name)
        if not ota_url(url):
            raise ValueError("snapshot target URL does not end with .velaterm-ota: %s" % url)
        validate_snapshot_mirror_location(target, asset_name)

        ref = DownloadRef(url=url, sha256=sha256.lower(), asset_name=asset_name, length=length)
        existing = refs_by_url.get(url)
        if existing:
            if existing.sha256 != ref.sha256 or existing.length != ref.length:
                raise ValueError("conflicting snapshot target metadata for %s" % url)
            continue
        same_name = refs_by_name.get(asset_name)
        if same_name and same_name.sha256 != ref.sha256:
            raise ValueError("asset name collision for %s with different sha256 values" % asset_name)
        refs_by_url[url] = ref
        refs_by_name.setdefault(asset_name, ref)
    return refs_by_url


def collect_optional_root_download_refs(optional_metadata):
    return {}


def add_download_ref(refs_by_url, ref):
    existing = refs_by_url.get(ref.url)
    if existing:
        if existing.sha256 != ref.sha256:
            raise ValueError("conflicting download metadata for %s" % ref.url)
        if (
            existing.length is not None
            and ref.length is not None
            and existing.length != ref.length
        ):
            raise ValueError("conflicting download length for %s" % ref.url)
        return existing
    refs_by_url[ref.url] = ref
    return ref


def first_target_url(target):
    locations = target.get("locations")
    if not isinstance(locations, list):
        return ""
    for location in locations:
        if isinstance(location, dict) and isinstance(location.get("url"), str):
            return location["url"]
    return ""


def validate_snapshot_mirror_location(target, asset_name):
    locations = target.get("locations")
    if not isinstance(locations, list):
        raise ValueError("snapshot target locations must be an array")
    expected_path = "assets/%s" % asset_name
    for location in locations:
        if not isinstance(location, dict):
            raise ValueError("snapshot target location must be a JSON object")
        if location.get("kind") == "snapshotMirror" and location.get("path") == expected_path:
            return
    raise ValueError("snapshot target %s is missing snapshotMirror path %s" % (asset_name, expected_path))


def fetch_optional_root_metadata(remote_base_url, timeout, github_proxy=""):
    remote_base_url = remote_base_url.rstrip("/") + "/"
    metadata = {}
    for name in OPTIONAL_ROOT_METADATA_FILES:
        url = urllib.parse.urljoin(remote_base_url, name)
        try:
            metadata[name] = json.loads(fetch_bytes(url, timeout, github_proxy=github_proxy).decode("utf-8"))
        except Exception as error:
            if not is_missing_optional_file(error):
                raise RuntimeError("failed to fetch optional metadata %s: %s" % (url, error))
    return metadata


def is_missing_optional_file(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 404
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, FileNotFoundError)
    return isinstance(error, FileNotFoundError)


def ota_url(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.path.endswith(".velaterm-ota")


def valid_asset_name(value):
    return (
        isinstance(value, str)
        and value
        and value not in (".", "..")
        and "/" not in value
        and "\\" not in value
        and value.endswith(".velaterm-ota")
    )


def valid_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def valid_relative_metadata_path(value):
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return False
    if "\\" in value:
        return False
    for component in value.split("/"):
        decoded = urllib.parse.unquote(component)
        if not decoded or decoded in (".", "..") or "/" in decoded or "\\" in decoded:
            return False
    return True


def is_sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def unique_refs_by_asset(refs_by_url):
    refs_by_name = {}
    for ref in refs_by_url.values():
        existing = refs_by_name.get(ref.asset_name)
        if existing:
            if existing.sha256 != ref.sha256:
                raise ValueError("asset name collision for %s with different sha256 values" % ref.asset_name)
            if (
                existing.length is not None
                and ref.length is not None
                and existing.length != ref.length
            ):
                raise ValueError("asset name collision for %s with different lengths" % ref.asset_name)
            continue
        refs_by_name[ref.asset_name] = ref
    return list(refs_by_name.values())


def digest_metadata(metadata):
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reusable_v2_snapshot(snapshot_dir, metadata, refs):
    if not v2_snapshot_metadata_matches(snapshot_dir, metadata):
        return False
    for ref in refs:
        asset_path = snapshot_dir / "assets" / ref.asset_name
        if not asset_matches(asset_path, ref):
            return False
    return True


def v2_snapshot_metadata_matches(snapshot_dir, metadata):
    expected_names = set(metadata)
    stale_names = set(OPTIONAL_ROOT_METADATA_FILES) - expected_names
    stale_names.update(("latest.json", "manifest.json"))
    for name in stale_names:
        if (snapshot_dir / name).exists():
            return False

    for name, expected in metadata.items():
        path = snapshot_dir / name
        if not path.is_file():
            return False
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if existing != expected:
            return False
    return True


def repair_v2_snapshot_assets(snapshot_dir, refs, timeout, github_proxy=""):
    downloaded = 0
    reused = 0
    assets_dir = snapshot_dir / "assets"
    for ref in refs:
        asset_path = assets_dir / ref.asset_name
        if not asset_matches(asset_path, ref):
            ensure_asset(ref, assets_dir, timeout, github_proxy=github_proxy)
            downloaded += 1
        else:
            reused += 1
    return {"downloaded": downloaded, "reused": reused}


def write_v2_current_state(cache_dir, remote_base_url, public_base_url, metadata, refs, snapshot_name):
    write_json_atomic(
        cache_dir / "current.json",
        {
            "snapshot": snapshot_name,
            "updatedAt": current_timestamp(),
        },
    )
    write_json_atomic(
        cache_dir / "state.json",
        {
            "remoteBaseUrl": remote_base_url.rstrip("/"),
            "publicBaseUrl": public_base_url,
            "metadataDigest": digest_metadata(metadata),
            "metadataFiles": sorted(metadata.keys()),
            "assetCount": len(refs),
            "snapshot": snapshot_name,
            "lastSyncedAt": current_timestamp(),
        },
    )


def copy_existing_asset(cache_dir, ref, target):
    snapshot_name = ""
    try:
        snapshot_name = current_snapshot_name(cache_dir)
    except Exception:
        return False
    source = snapshot_path(cache_dir, snapshot_name) / "assets" / ref.asset_name
    if not asset_matches(source, ref):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def asset_matches(path, ref):
    path = Path(path)
    if not path.exists():
        return False
    if ref.length is not None and path.stat().st_size != ref.length:
        return False
    return sha256_file(path) == ref.sha256


def ensure_asset(ref, assets_dir, timeout, github_proxy=""):
    target = assets_dir / ref.asset_name
    if asset_matches(target, ref):
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        digest, length = download_to_file(ref.url, tmp, timeout, github_proxy=github_proxy)
        if digest != ref.sha256:
            raise ValueError(
                "sha256 mismatch for %s: expected %s, got %s"
                % (ref.url, ref.sha256, digest),
            )
        if ref.length is not None and length != ref.length:
            raise ValueError(
                "length mismatch for %s: expected %s, got %s"
                % (ref.url, ref.length, length),
            )
        os.replace(str(tmp), str(target))
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


def download_to_file(url, target, timeout, github_proxy=""):
    hasher = hashlib.sha256()
    length = 0
    with open_url(url, timeout, github_proxy=github_proxy) as response:
        with target.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                length += len(chunk)
                output.write(chunk)
    return hasher.hexdigest(), length


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def fetch_bytes(url, timeout, github_proxy=""):
    with open_url(url, timeout, github_proxy=github_proxy) as response:
        return response.read()


def open_url(url, timeout, github_proxy=""):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    proxy_url = github_proxy_for_url(url, github_proxy)
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
        )
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def github_proxy_for_url(url, github_proxy):
    if not github_proxy:
        return ""
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return ""
    host = host.lower()
    for domain in GITHUB_PROXY_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return github_proxy
    return ""


def resolve_github_proxy(args):
    proxy = normalize_proxy_url(getattr(args, "github_proxy", "") or "")
    if not proxy:
        return ""
    username = getattr(args, "github_proxy_username", "") or ""
    password = getattr(args, "github_proxy_password", "") or ""
    if not username and not password:
        return proxy

    parsed = urllib.parse.urlsplit(proxy)
    if "@" in parsed.netloc:
        return proxy

    auth = "%s:%s@" % (
        urllib.parse.quote(username, safe=""),
        urllib.parse.quote(password, safe=""),
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme, auth + parsed.netloc, parsed.path, parsed.query, parsed.fragment),
    )


def normalize_proxy_url(proxy):
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        return "http://" + proxy
    return proxy


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def current_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def resolve_cache_dir(value):
    if value:
        return Path(value).expanduser().resolve()
    return DEFAULT_CACHE_DIR


def apply_config(args):
    config = read_config(args.config)
    apply_string_config(args, config, "remote_base_url", DEFAULT_REMOTE_BASE_URL)
    apply_string_config(args, config, "cache_dir", "")
    apply_string_config(args, config, "base_url", "")
    apply_string_config(args, config, "advertise_host", "")
    apply_string_config(args, config, "github_proxy", "")
    apply_string_config(args, config, "github_proxy_username", "")
    apply_string_config(args, config, "github_proxy_password", "")
    apply_string_config(args, config, "metadata_public_key_file", "")
    apply_int_config(args, config, "port", 18080)
    apply_int_config(args, config, "timeout", 30)
    if hasattr(args, "bind"):
        apply_string_config(args, config, "bind", "0.0.0.0")
    if hasattr(args, "interval"):
        apply_int_config(args, config, "interval", 300)


def read_config(config_path):
    required = bool(config_path)
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        if required:
            raise RuntimeError("config file does not exist: %s" % path)
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("config file must contain a JSON object: %s" % path)
    return value


def apply_string_config(args, config, name, default):
    current = getattr(args, name, None)
    if current is not None:
        setattr(args, name, current)
        return
    value = config.get(name, default)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RuntimeError("config field %s must be a string" % name)
    setattr(args, name, value)


def apply_int_config(args, config, name, default):
    current = getattr(args, name, None)
    if current is not None:
        setattr(args, name, current)
        return
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("config field %s must be an integer" % name)
    if value <= 0:
        raise RuntimeError("config field %s must be greater than zero" % name)
    setattr(args, name, value)


def public_base_url(args):
    if args.base_url:
        return args.base_url.rstrip("/")
    host = args.advertise_host or detect_lan_ip()
    return "http://%s:%s" % (host, args.port)


def detect_lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def require_current_snapshot(cache_dir):
    snapshot_dir = snapshot_path(cache_dir, current_snapshot_name(cache_dir))
    missing = [name for name in REQUIRED_SNAPSHOT_METADATA_FILES if not (snapshot_dir / name).exists()]
    if missing:
        raise RuntimeError(
            "local mirror snapshot is incomplete; missing %s. Run the sync command first."
            % ", ".join(missing),
        )
    return snapshot_dir


def current_snapshot_name(cache_dir):
    pointer_path = Path(cache_dir) / "current.json"
    if not pointer_path.exists():
        raise RuntimeError("local mirror is not synced yet; missing current.json. Run the sync command first.")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    snapshot_name = pointer.get("snapshot")
    if not valid_snapshot_name(snapshot_name):
        raise RuntimeError("invalid current snapshot pointer in %s" % pointer_path)
    return snapshot_name


def valid_snapshot_name(value):
    if not isinstance(value, str) or not value:
        return False
    return "/" not in value and "\\" not in value and value not in (".", "..")


def snapshot_path(cache_dir, snapshot_name):
    return Path(cache_dir) / "snapshots" / snapshot_name


class OtaMirrorRequestHandler(http.server.SimpleHTTPRequestHandler):
    cache_dir = None

    def send_head(self):
        if self.is_root_timestamp_request():
            return self.send_timestamp_head()
        return super().send_head()

    def translate_path(self, path):
        path = path.split("?", 1)[0]
        path = path.split("#", 1)[0]
        path = posixpath.normpath(urllib.parse.unquote(path))
        parts = [part for part in path.split("/") if part and part not in (".", "..")]
        not_found = Path(self.cache_dir) / ".not-found"

        if len(parts) == 1 and parts[0] in ROOT_CURRENT_METADATA_FILES:
            return str(require_current_snapshot(self.cache_dir) / parts[0])

        if (
            len(parts) == 3
            and parts[0] == "snapshots"
            and valid_snapshot_name(parts[1])
            and parts[2] == "snapshot.json"
        ):
            return str(Path(self.cache_dir) / "snapshots" / parts[1] / "snapshot.json")

        if (
            len(parts) == 4
            and parts[0] == "snapshots"
            and valid_snapshot_name(parts[1])
            and parts[2] == "assets"
            and valid_asset_name(parts[3])
        ):
            return str(Path(self.cache_dir) / "snapshots" / parts[1] / "assets" / parts[3])

        return str(not_found)

    def is_root_timestamp_request(self):
        path = self.path.split("?", 1)[0]
        path = path.split("#", 1)[0]
        path = posixpath.normpath(urllib.parse.unquote(path))
        parts = [part for part in path.split("/") if part and part not in (".", "..")]
        return parts == ["timestamp.json"]

    def send_timestamp_head(self):
        try:
            snapshot_name = current_snapshot_name(self.cache_dir)
            snapshot_dir = snapshot_path(self.cache_dir, snapshot_name)
            timestamp_path = snapshot_dir / "timestamp.json"
            if not timestamp_path.is_file():
                self.send_error(404, "File not found")
                return None
            timestamp = json.loads(timestamp_path.read_text(encoding="utf-8"))
            timestamp = rewrite_timestamp_for_mirror_serving(timestamp)
            encoded = (json.dumps(timestamp, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except Exception as error:
            self.send_error(500, str(error))
            return None

        response = io.BytesIO(encoded)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return response

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path.endswith(".json"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))


def make_handler(cache_dir):
    class Handler(OtaMirrorRequestHandler):
        pass

    Handler.cache_dir = Path(cache_dir)

    return Handler


def rewrite_timestamp_for_mirror_serving(timestamp):
    if not isinstance(timestamp, dict):
        return timestamp
    rewritten = dict(timestamp)
    site = timestamp.get("site")
    if isinstance(site, dict):
        site = dict(site)
    else:
        site = {}
    site["kind"] = "mirror"
    rewritten["site"] = site
    return rewritten


def request_base_url(handler):
    host = handler.headers.get("Host")
    if not host:
        address, port = handler.server.server_address[:2]
        host = "%s:%s" % (address, port)
    return "http://%s" % host


def local_snapshot_asset_url(base_url, snapshot_name, asset_name):
    return "%s/snapshots/%s/assets/%s" % (
        base_url.rstrip("/"),
        urllib.parse.quote(snapshot_name, safe=""),
        urllib.parse.quote(asset_name, safe=""),
    )


def serve_mirror(cache_dir, bind, port):
    require_current_snapshot(cache_dir)
    server = http.server.ThreadingHTTPServer((bind, port), make_handler(cache_dir))
    return server


def build_server(bind, port, cache_dir):
    return serve_mirror(cache_dir=cache_dir, bind=bind, port=port)


def run_polling_mirror(args):
    cache_dir = resolve_cache_dir(args.cache_dir)
    base_url = public_base_url(args)
    github_proxy = resolve_github_proxy(args)
    metadata_verifier = build_metadata_verifier(args.metadata_public_key_file)
    sync_lock = threading.Lock()
    stop_event = threading.Event()

    def sync_once(label):
        with sync_lock:
            result = sync_mirror(
                args.remote_base_url,
                cache_dir,
                base_url,
                timeout=args.timeout,
                github_proxy=github_proxy,
                metadata_verifier=metadata_verifier,
            )
        print(
            "%s: synced %s metadata files, %s assets (%s downloaded, %s reused)"
            % (label, result.metadata_count, result.asset_count, result.download_count, result.reused_count),
            flush=True,
        )

    try:
        sync_once("initial")
    except Exception as error:
        try:
            require_current_snapshot(cache_dir)
        except Exception:
            raise
        print("initial sync failed, serving existing mirror: %s" % error, file=sys.stderr)

    server = serve_mirror(cache_dir, args.bind, args.port)

    def poll_loop():
        while not stop_event.wait(args.interval):
            try:
                sync_once("poll")
            except Exception as error:
                print("poll sync failed: %s" % error, file=sys.stderr, flush=True)

    thread = threading.Thread(target=poll_loop, name="ota-mirror-poller", daemon=True)
    thread.start()
    print_server_urls(base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


def print_server_urls(base_url):
    base_url = base_url.rstrip("/")
    print("Local OTA mirror serving:", flush=True)
    print("  timestamp:     %s/timestamp.json" % base_url, flush=True)
    print("  snapshot:      %s/snapshots/<snapshot-id>/snapshot.json" % base_url, flush=True)
    print("  assets:        %s/snapshots/<snapshot-id>/assets/<name>.velaterm-ota" % base_url, flush=True)


def command_sync(args):
    github_proxy = resolve_github_proxy(args)
    result = sync_mirror(
        remote_base_url=args.remote_base_url,
        cache_dir=resolve_cache_dir(args.cache_dir),
        public_base_url=public_base_url(args),
        timeout=args.timeout,
        github_proxy=github_proxy,
        metadata_verifier=build_metadata_verifier(args.metadata_public_key_file),
    )
    print(
        "synced %s metadata files, %s assets (%s downloaded, %s reused)"
        % (result.metadata_count, result.asset_count, result.download_count, result.reused_count),
    )


def command_serve(args):
    base_url = public_base_url(args)
    server = serve_mirror(resolve_cache_dir(args.cache_dir), args.bind, args.port)
    print_server_urls(base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        server.server_close()


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_metadata_verifier(metadata_public_key_file):
    if not metadata_public_key_file:
        return None
    public_key = decode_hex_or_base64_text(
        Path(metadata_public_key_file).read_text(encoding="utf-8"),
        "metadata public key",
    )
    if len(public_key) != 32:
        raise RuntimeError("metadata public key must be 32 bytes")

    def verify(name, text, signature):
        del signature
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise RuntimeError("metadata must be a JSON object")
            signature_text = value.get("signature")
            if not isinstance(signature_text, str) or not signature_text:
                raise RuntimeError("metadata signature is required")
            signature_bytes = decode_hex_or_base64_text(signature_text, "metadata signature")
            if len(signature_bytes) != 64:
                raise RuntimeError("metadata signature must be 64 bytes")
            unsigned_value = strip_signature_fields(
                value,
                strip_root_site=name == "timestamp.json",
            )
            verify_ed25519(
                canonical_json_bytes(unsigned_value),
                signature_bytes,
                public_key,
            )
        except Exception as error:
            raise RuntimeError("failed to verify metadata %s: %s" % (name, error)) from error

    return verify


def decode_hex_or_base64_text(value, label):
    text = "".join(value.split())
    if not text:
        raise RuntimeError("%s is empty" % label)
    try:
        if len(text) % 2 == 0:
            return bytes.fromhex(text)
    except ValueError:
        pass
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as error:
        raise RuntimeError("%s must be hex or base64" % label) from error


def strip_signature_fields(value, strip_root_site=False, depth=0):
    if isinstance(value, list):
        return [
            strip_signature_fields(item, strip_root_site=strip_root_site, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: strip_signature_fields(child, strip_root_site=strip_root_site, depth=depth + 1)
            for key, child in value.items()
            if key != "signature" and not (strip_root_site and depth == 0 and key == "site")
        }
    return value


def canonical_json_bytes(value):
    return canonical_json(value).encode("utf-8")


def canonical_json(value):
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            + ":"
            + canonical_json(value[key])
            for key in sorted(value.keys())
        ) + "}"
    if isinstance(value, float) and not math_is_finite(value):
        raise RuntimeError("unsupported value for canonical JSON: non-finite number")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def math_is_finite(value):
    return value == value and value not in (float("inf"), float("-inf"))


def verify_ed25519(message, signature, public_key):
    if len(signature) != 64:
        raise RuntimeError("metadata signature must be 64 bytes")
    if len(public_key) != 32:
        raise RuntimeError("metadata public key must be 32 bytes")
    r_bytes = signature[:32]
    s = int.from_bytes(signature[32:], "little")
    if s >= ED25519_Q:
        raise RuntimeError("metadata signature scalar is out of range")

    try:
        a_point = ed25519_decode_point(public_key)
        r_point = ed25519_decode_point(r_bytes)
    except RuntimeError as error:
        raise RuntimeError("decode metadata signature point: %s" % error) from error

    h = int.from_bytes(
        hashlib.sha512(r_bytes + public_key + message).digest(),
        "little",
    ) % ED25519_Q
    left = ed25519_scalar_mult(ed25519_base_point(), s)
    right = ed25519_point_add(r_point, ed25519_scalar_mult(a_point, h))
    if left != right:
        raise RuntimeError("verify metadata signature")


def ed25519_base_point():
    return (ed25519_recover_x(ED25519_BASE_Y, 0), ED25519_BASE_Y)


def ed25519_decode_point(encoded):
    if len(encoded) != 32:
        raise RuntimeError("encoded point must be 32 bytes")
    y = int.from_bytes(encoded, "little") & ((1 << 255) - 1)
    sign = encoded[31] >> 7
    if y >= ED25519_P:
        raise RuntimeError("encoded point y is out of range")
    x = ed25519_recover_x(y, sign)
    if not ed25519_is_on_curve((x, y)):
        raise RuntimeError("encoded point is not on curve")
    return (x, y)


def ed25519_recover_x(y, sign):
    y2 = y * y % ED25519_P
    numerator = (y2 - 1) % ED25519_P
    denominator = (ED25519_D * y2 + 1) % ED25519_P
    x2 = numerator * pow(denominator, ED25519_P - 2, ED25519_P) % ED25519_P
    x = pow(x2, (ED25519_P + 3) // 8, ED25519_P)
    if (x * x - x2) % ED25519_P != 0:
        x = x * ED25519_I % ED25519_P
    if (x * x - x2) % ED25519_P != 0:
        raise RuntimeError("invalid point x coordinate")
    if (x & 1) != sign:
        x = ED25519_P - x
    return x


def ed25519_is_on_curve(point):
    x, y = point
    return (
        (y * y - x * x - 1 - ED25519_D * x * x * y * y)
        % ED25519_P
        == 0
    )


def ed25519_point_add(left, right):
    x1, y1 = left
    x2, y2 = right
    x1x2 = x1 * x2 % ED25519_P
    y1y2 = y1 * y2 % ED25519_P
    dxxyy = ED25519_D * x1x2 * y1y2 % ED25519_P
    x3 = (x1 * y2 + x2 * y1) * pow(1 + dxxyy, ED25519_P - 2, ED25519_P)
    y3 = (y1y2 + x1x2) * pow(1 - dxxyy, ED25519_P - 2, ED25519_P)
    return (x3 % ED25519_P, y3 % ED25519_P)


def ed25519_scalar_mult(point, scalar):
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = ed25519_point_add(result, addend)
        addend = ed25519_point_add(addend, addend)
        scalar >>= 1
    return result


def add_common_sync_args(parser):
    parser.add_argument(
        "--config",
        default=None,
        help="JSON config file (default: subrepos/ota-site/.local-ota/config.json if it exists)",
    )
    parser.add_argument(
        "--remote-base-url",
        default=None,
        help="remote OTA site base URL (default: https://acer-0606.github.io/velaterm-ota-site)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="local mirror cache directory (default: subrepos/ota-site/.local-ota)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="public URL displayed for clients; overrides --advertise-host",
    )
    parser.add_argument(
        "--advertise-host",
        default=None,
        help="LAN host/IP displayed for clients when --base-url is not set",
    )
    parser.add_argument(
        "--github-proxy",
        default=None,
        help="proxy URL used only for GitHub metadata and asset requests",
    )
    parser.add_argument(
        "--github-proxy-username",
        default=None,
        help="optional GitHub proxy username; prefer config files for real credentials",
    )
    parser.add_argument(
        "--github-proxy-password",
        default=None,
        help="optional GitHub proxy password; prefer config files for real credentials",
    )
    parser.add_argument(
        "--metadata-public-key-file",
        default=None,
        help="public key file used to verify v2 metadata signatures",
    )
    parser.add_argument("--port", type=positive_int, default=None, help="LAN HTTP port")
    parser.add_argument("--timeout", type=positive_int, default=None, help="network timeout in seconds")


def add_common_serve_args(parser):
    parser.add_argument(
        "--config",
        default=None,
        help="JSON config file (default: subrepos/ota-site/.local-ota/config.json if it exists)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="local mirror cache directory (default: subrepos/ota-site/.local-ota)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="URL to display for clients; overrides --advertise-host",
    )
    parser.add_argument(
        "--advertise-host",
        default=None,
        help="LAN host/IP to display when --base-url is not set",
    )
    parser.add_argument("--bind", default=None, help="address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=positive_int, default=None, help="LAN HTTP port")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="sync metadata and OTA packages once")
    add_common_sync_args(sync_parser)
    sync_parser.set_defaults(func=command_sync)

    serve_parser = subparsers.add_parser("serve", help="serve an existing local mirror")
    add_common_serve_args(serve_parser)
    serve_parser.set_defaults(func=command_serve)

    run_parser = subparsers.add_parser("run", help="sync once, serve, then poll for updates")
    add_common_sync_args(run_parser)
    run_parser.add_argument("--bind", default=None, help="address to bind (default: 0.0.0.0)")
    run_parser.add_argument(
        "--interval",
        type=positive_int,
        default=None,
        help="poll interval in seconds (default: 300)",
    )
    run_parser.set_defaults(func=run_polling_mirror)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        apply_config(args)
        args.func(args)
    except Exception as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
