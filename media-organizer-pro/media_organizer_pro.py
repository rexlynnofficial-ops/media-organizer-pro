#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import Tk, StringVar, BooleanVar, IntVar, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Iterable, Optional
import xml.etree.ElementTree as ET

APP_NAME = "Media Organizer Pro"
APP_VERSION = "13.0 Device Fingerprint Unknowns"
UNKNOWN_CAMERA = "No Camera Metadata"
PARTIAL_SUFFIX = ".mop-partial"

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif",
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf",
    ".rw2", ".pef", ".ptx", ".rwl", ".3fr", ".fff", ".iiq", ".x3f", ".dng", ".raw"
}
VIDEO_EXTS = {
    ".mxf", ".mov", ".mp4", ".mkv", ".avi", ".m4v", ".flv", ".wmv", ".mts", ".m2ts",
    ".braw", ".r3d", ".crm", ".ari", ".mpg", ".mpeg"
}
AUDIO_EXTS = {".wav", ".wave", ".aif", ".aiff", ".mp3", ".flac", ".aac", ".m4a", ".wma", ".ogg", ".ape"}
SIDECAR_EXTS = {".xml", ".xmp", ".thm", ".smi", ".json", ".cpi", ".lrf", ".lrv", ".aae"}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS | AUDIO_EXTS
SCAN_EXTS = MEDIA_EXTS | SIDECAR_EXTS

CAMERA_KEYS = (
    "CameraModelName", "Model", "UniqueCameraModel", "CameraModel", "DeviceModelName",
    "AndroidModel", "SonyModelID", "CanonModelID", "NikonModelID"
)
DATE_KEYS = (
    "DateTimeOriginal", "SubSecDateTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate",
    "ContentCreateDate", "DateTimeCreated", "FileCreateDate"
)


def subprocess_env_for_perl_tools() -> dict[str, str]:
    """Return a macOS-safe environment for Perl-based command-line tools.

    Some launchers export the Linux-style ``C.UTF-8`` locale, which macOS Perl
    does not support. ExifTool then aborts before it can inspect a file. The
    portable C locale avoids that startup failure; JSON decoding remains
    explicitly UTF-8 in the caller.
    """
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
    return env


def find_executable(name: str, macos_candidates: tuple[str, ...] = ()) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "darwin":
        for candidate in macos_candidates:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def paths_overlap(first: Path, second: Path) -> bool:
    """Return whether two folder trees intersect or one contains the other."""
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    return first_resolved == second_resolved or first_resolved in second_resolved.parents or second_resolved in first_resolved.parents


def safe_name(value: str, fallback: str = UNKNOWN_CAMERA) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:120] or fallback


def normalize_camera(make: str, model: str) -> str:
    make = re.sub(r"\s+", " ", (make or "").strip())
    model = re.sub(r"\s+", " ", (model or "").strip())
    if not model:
        return UNKNOWN_CAMERA
    aliases = {
        "ILCE-7M4": "Sony α7 IV", "ILCE-7SM3": "Sony α7S III", "ILME-FX3": "Sony FX3",
        "ILME-FX30": "Sony FX30", "ILME-FX6V": "Sony FX6", "ILME-FX6": "Sony FX6",
        "DC-S5M2": "Panasonic Lumix S5 II", "DC-S5M2X": "Panasonic Lumix S5 IIX",
    }
    if model.upper() in aliases:
        return aliases[model.upper()]
    if make and model.lower().startswith(make.lower()):
        return safe_name(model)
    return safe_name(f"{make} {model}" if make else model)


def device_fingerprint(raw: dict | None, category: str, make: str = "", extra: str = "") -> str:
    """Return a stable short identity for an unresolved recording device.

    Deliberately ignores path, filename, timestamps and clip-specific values so files
    from the same unidentified device group together, while differing metadata
    signatures separate different devices.
    """
    raw = raw or {}
    useful_names = (
        "make", "manufacturer", "vendor", "company", "originator", "software",
        "firmware", "encoder", "compressor", "device", "model", "product",
        "machine", "handler", "majorbrand", "compatiblebrands", "format",
        "codec", "audioformat", "videocodec", "creationtool", "writingapplication"
    )
    ignored = ("date", "time", "duration", "filename", "filepath", "directory",
               "sourcefile", "filesize", "filemodify", "fileaccess", "filecreate")
    parts = [category.strip().lower(), (make or "").strip().lower(), (extra or "").strip().lower()]
    for key in sorted(raw, key=lambda x: str(x).lower()):
        lk = str(key).lower()
        if any(token in lk for token in ignored):
            continue
        if not any(token in lk for token in useful_names):
            continue
        value = str(raw.get(key, "") or "").strip().lower()
        if value and value not in {"-", "unknown", "none", "n/a"}:
            parts.append(f"{lk}={value[:180]}")
    seed = "|".join(parts)
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:8].upper()


def unresolved_device_label(category: str, raw: dict | None, make: str = "", extra: str = "") -> str:
    fp = device_fingerprint(raw, category, make, extra)
    vendor = safe_name(make, "").strip()
    if category == "video":
        return f"{vendor} Camera — Unknown Model [{fp}]" if vendor else f"Unknown Video Camera [{fp}]"
    if category == "audio":
        return f"{vendor} Recorder — Unknown Model [{fp}]" if vendor else f"Unknown Audio Recorder [{fp}]"
    return f"Unknown Device [{fp}]"


def parse_exif_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    cleaned = str(value).strip()
    cleaned = re.sub(r"([+-]\d\d:\d\d|Z)$", "", cleaned).strip()
    cleaned = cleaned.split(".")[0]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            pass
    return None


def format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


@dataclass(frozen=True)
class JobConfig:
    sources: tuple[str, ...]
    destinations: tuple[str, ...]
    operation: str
    group_camera: bool
    group_date: bool
    date_structure: str
    checksum: str
    include_sidecars: bool
    generate_report: bool
    duplicate_policy: str = "skip"
    report_formats: tuple[str, ...] = ("csv", "json", "html")


@dataclass
class Metadata:
    camera: str = UNKNOWN_CAMERA
    make: str = ""
    model: str = ""
    lens: str = ""
    capture_date: str = ""
    metadata_source: str = "filesystem"
    raw: dict | None = None


@dataclass
class ClipPackage:
    package_id: str
    primary: Path
    members: list[Path]
    media_type: str
    metadata: Metadata
    association: str
    camera_confidence: str = "unknown"

    @property
    def total_size(self) -> int:
        return sum(p.stat().st_size for p in self.members if p.exists())


@dataclass
class TransferRecord:
    job_id: str
    source: str
    destination: str
    relative_destination: str
    filename: str
    media_type: str
    size: int
    camera: str
    capture_date: str
    checksum_algorithm: str
    source_checksum: str
    destination_checksum: str
    status: str
    error: str
    started_at: str
    completed_at: str


class CancelledError(Exception):
    pass


class MetadataEngine:
    def __init__(self, cache_db: "AssetDatabase"):
        self.db = cache_db
        self.exiftool = find_executable(
            "exiftool",
            ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"),
        )

    def read(self, path: Path) -> Metadata:
        stat = path.stat()
        cached = self.db.get_metadata(str(path), stat.st_size, stat.st_mtime_ns)
        if cached:
            return Metadata(**cached)

        metadata = self._read_exiftool(path) if self.exiftool else None
        if metadata is None and sys.platform == "darwin":
            metadata = self._read_mdls(path)
        if metadata is None:
            metadata = Metadata(capture_date=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"))
        self.db.put_metadata(str(path), stat.st_size, stat.st_mtime_ns, asdict(metadata))
        return metadata

    def _read_exiftool(self, path: Path) -> Optional[Metadata]:
        try:
            proc = subprocess.run(
                [self.exiftool, "-j", "-G1", "-a", "-s", "-charset", "filename=UTF8", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=False, env=subprocess_env_for_perl_tools()
            )
            if proc.returncode not in (0, 1) or not proc.stdout.strip():
                return None
            payload = json.loads(proc.stdout)[0]
            flattened = {}
            for key, val in payload.items():
                simple = key.split(":")[-1]
                if simple not in flattened or not flattened[simple]:
                    flattened[simple] = val
            make = str(flattened.get("Make", "") or "").strip()
            model = ""
            for key in CAMERA_KEYS:
                value = flattened.get(key)
                if value and str(value).strip() not in ("-", "Unknown"):
                    model = str(value).strip()
                    break
            lens = str(flattened.get("LensModel") or flattened.get("LensID") or "").strip()
            dt = None
            for key in DATE_KEYS:
                dt = parse_exif_date(str(flattened.get(key, "") or ""))
                if dt:
                    break
            return Metadata(
                camera=normalize_camera(make, model), make=make, model=model, lens=lens,
                capture_date=(dt.isoformat(timespec="seconds") if dt else ""),
                metadata_source="ExifTool", raw=flattened
            )
        except Exception:
            return None

    def _read_mdls(self, path: Path) -> Optional[Metadata]:
        keys = ["kMDItemAcquisitionMake", "kMDItemAcquisitionModel", "kMDItemContentCreationDate"]
        try:
            proc = subprocess.run(["mdls", "-plist", "-", *sum((["-name", k] for k in keys), []), str(path)],
                                  capture_output=True, timeout=15, check=False)
            if proc.returncode != 0 or not proc.stdout:
                return None
            import plistlib
            data = plistlib.loads(proc.stdout)
            make = str(data.get("kMDItemAcquisitionMake", "") or "")
            model = str(data.get("kMDItemAcquisitionModel", "") or "")
            raw_dt = data.get("kMDItemContentCreationDate")
            dt = raw_dt if isinstance(raw_dt, datetime) else None
            return Metadata(camera=normalize_camera(make, model), make=make, model=model,
                            capture_date=dt.isoformat(timespec="seconds") if dt else "",
                            metadata_source="macOS Spotlight", raw={k: str(v) for k, v in data.items()})
        except Exception:
            return None


class VideoMetadataResolver:
    """Resolve video camera identity from primary media, sidecars and conservative fingerprints."""
    MODEL_KEYS = (
        "CameraModelName", "Model", "DeviceModelName", "DeviceModel", "ProductName",
        "ModelName", "CameraModel", "RecorderModel", "CaptureDeviceModel"
    )
    MAKE_KEYS = ("Make", "Manufacturer", "DeviceManufacturer", "CompanyName", "Vendor")

    def __init__(self, metadata_engine: MetadataEngine):
        self.metadata_engine = metadata_engine
        self.ffprobe = find_executable(
            "ffprobe",
            ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"),
        )

    @staticmethod
    def _all_text(payload: dict | None) -> str:
        if not payload:
            return ""
        return " ".join(str(v) for v in payload.values() if v is not None)

    def resolve(self, primary: Path, members: list[Path], base: Metadata) -> tuple[Metadata, str]:
        evidence = []
        make, model = base.make, base.model
        raw = dict(base.raw or {})
        text = self._all_text(raw)

        if model and base.camera != UNKNOWN_CAMERA:
            return base, "confirmed"

        # XML/XMP sidecars are often the authoritative camera source for MXF workflows.
        for member in members:
            if member.suffix.lower() not in {".xml", ".xmp"}:
                continue
            fields = self._read_xml_fields(member)
            raw.update({f"Sidecar:{k}": v for k, v in fields.items()})
            for key in self.MAKE_KEYS:
                if not make and fields.get(key): make = fields[key]
            for key in self.MODEL_KEYS:
                if not model and fields.get(key): model = fields[key]
            if fields:
                evidence.append(f"sidecar:{member.name}")

        # ffprobe supplements container tags when ExifTool does not expose a model.
        if not model and self.ffprobe:
            tags = self._read_ffprobe(primary)
            raw.update({f"ffprobe:{k}": v for k, v in tags.items()})
            for key in self.MAKE_KEYS:
                if not make and tags.get(key): make = tags[key]
            for key in self.MODEL_KEYS:
                if not model and tags.get(key): model = tags[key]
            if tags:
                evidence.append("ffprobe")

        combined = (text + " " + self._all_text(raw) + " " + primary.name).lower()
        known = [
            (("osmo action 5 pro", "action 5 pro", "oa5"), "DJI", "Osmo Action 5 Pro"),
            (("osmo action 4", "action 4"), "DJI", "Osmo Action 4"),
            (("osmo pocket 3", "pocket 3"), "DJI", "Osmo Pocket 3"),
            (("ilme-fx3", "sony fx3"), "Sony", "FX3"),
            (("ilme-fx30", "sony fx30"), "Sony", "FX30"),
            (("ilme-fx6", "sony fx6"), "Sony", "FX6"),
            (("canon eos r5 c", "eos r5 c"), "Canon", "EOS R5 C"),
        ]
        if not model:
            for tokens, kmake, kmodel in known:
                if any(token in combined for token in tokens):
                    make, model = kmake, kmodel
                    evidence.append("metadata fingerprint")
                    break

        # Conservative fallback: identify manufacturer without fabricating a model.
        if not model and (primary.name.upper().startswith("DJI_") or " dji " in f" {combined} "):
            make, model = "DJI", "Camera (model unresolved)"
            evidence.append("DJI filename/container fingerprint")
        elif not model and "sony" in combined:
            make, model = "Sony", "Camera (model unresolved)"
            evidence.append("Sony metadata fingerprint")
        elif not model and "canon" in combined:
            make, model = "Canon", "Camera (model unresolved)"
            evidence.append("Canon metadata fingerprint")

        dt = base.capture_date
        if model and "unresolved" not in model.lower():
            camera = normalize_camera(make, model)
            confidence = "confirmed"
        else:
            camera = unresolved_device_label("video", raw, make, primary.suffix.lower())
            confidence = "probable" if make or model else "unidentified"
        return Metadata(camera=camera, make=make, model=model, lens=base.lens,
                        capture_date=dt, metadata_source=" + ".join([base.metadata_source] + evidence), raw=raw), confidence

    def _read_xml_fields(self, path: Path) -> dict[str, str]:
        result = {}
        try:
            root = ET.parse(path).getroot()
            for elem in root.iter():
                tag = elem.tag.split("}")[-1]
                text = (elem.text or "").strip()
                if text and len(text) < 300 and tag not in result:
                    result[tag] = text
                for key, value in elem.attrib.items():
                    simple = key.split("}")[-1]
                    if value and simple not in result:
                        result[simple] = str(value)
        except Exception:
            pass
        return result

    def _read_ffprobe(self, path: Path) -> dict[str, str]:
        try:
            proc = subprocess.run([self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)],
                                  capture_output=True, text=True, timeout=30, check=False)
            if proc.returncode != 0 or not proc.stdout.strip(): return {}
            data = json.loads(proc.stdout)
            out = {}
            for key, value in (data.get("format", {}).get("tags", {}) or {}).items(): out[str(key)] = str(value)
            for stream in data.get("streams", []) or []:
                for key, value in (stream.get("tags", {}) or {}).items(): out.setdefault(str(key), str(value))
            return out
        except Exception:
            return {}


class AudioMetadataResolver:
    """Resolve recorder/device identity from BWF, WAV and container metadata."""
    MODEL_KEYS = ("RecorderModel", "DeviceModelName", "DeviceModel", "Model", "ProductName", "MachineType")
    MAKE_KEYS = ("Manufacturer", "Make", "DeviceManufacturer", "Originator", "CompanyName")

    def resolve(self, path: Path, base: Metadata) -> tuple[Metadata, str]:
        raw = dict(base.raw or {})
        combined = " ".join([path.name] + [str(v) for v in raw.values() if v is not None]).lower()
        make, model = base.make, base.model
        for key in self.MAKE_KEYS:
            if not make and raw.get(key): make = str(raw[key]).strip()
        for key in self.MODEL_KEYS:
            if not model and raw.get(key): model = str(raw[key]).strip()
        fingerprints = [
            (("mixpre-10 ii", "mixpre10 ii", "mixpre-10ii"), "Sound Devices", "MixPre-10 II"),
            (("mixpre-6 ii", "mixpre6 ii", "mixpre-6ii"), "Sound Devices", "MixPre-6 II"),
            (("mixpre-3 ii", "mixpre3 ii", "mixpre-3ii"), "Sound Devices", "MixPre-3 II"),
            (("833", "sound devices 833"), "Sound Devices", "833"),
            (("888", "sound devices 888"), "Sound Devices", "888"),
            (("scorpio",), "Sound Devices", "Scorpio"),
            (("zoom f8n pro", "f8n pro"), "Zoom", "F8n Pro"),
            (("zoom f8n", "f8n"), "Zoom", "F8n"),
            (("zoom f6",), "Zoom", "F6"),
            (("zoom h6",), "Zoom", "H6"),
            (("tascam portacapture x8", "portacapture x8", "portacapture_x8", "portacapturex8", "pc x8", "pc-x8"), "TASCAM", "Portacapture X8"),
            (("tascam portacapture x6", "portacapture x6", "portacapture_x6", "portacapturex6", "pc x6", "pc-x6"), "TASCAM", "Portacapture X6"),
            (("tascam dr-701d", "dr-701d"), "TASCAM", "DR-701D"),
            (("deity pr-2", "pr-2"), "Deity", "PR-2"),
        ]
        if not model:
            for tokens, kmake, kmodel in fingerprints:
                if any(token in combined for token in tokens):
                    make, model = kmake, kmodel
                    break
        if model:
            camera = normalize_camera(make, model)
            confidence = "confirmed" if any(str(raw.get(k, "")).strip() for k in self.MODEL_KEYS) else "probable"
        elif "sound devices" in combined:
            make, model, confidence = "Sound Devices", "", "probable"
            camera = unresolved_device_label("audio", raw, make, path.suffix.lower())
        elif "zoom" in combined:
            make, model, confidence = "Zoom", "", "probable"
            camera = unresolved_device_label("audio", raw, make, path.suffix.lower())
        elif "tascam" in combined or "teac" in combined:
            make, model, confidence = "TASCAM", "", "probable"
            camera = unresolved_device_label("audio", raw, make, path.suffix.lower())
        else:
            camera, confidence = unresolved_device_label("audio", raw, "", path.suffix.lower()), "unidentified"
        return Metadata(camera=camera, make=make, model=model, lens=base.lens, capture_date=base.capture_date,
                        metadata_source=base.metadata_source + " + audio/BWF resolver", raw=raw), confidence


class AssetDatabase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata_cache(
              path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, payload TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transfers(
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, source TEXT, destination TEXT,
              checksum TEXT, algorithm TEXT, status TEXT, completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_transfers_checksum ON transfers(checksum, algorithm, status);
            """)

    def get_metadata(self, path: str, size: int, mtime_ns: int) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute("SELECT payload FROM metadata_cache WHERE path=? AND size=? AND mtime_ns=?",
                                    (path, size, mtime_ns)).fetchone()
        return json.loads(row[0]) if row else None

    def put_metadata(self, path: str, size: int, mtime_ns: int, payload: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO metadata_cache VALUES(?,?,?,?,?)",
                              (path, size, mtime_ns, json.dumps(payload, ensure_ascii=False), datetime.now().isoformat()))

    def add_transfer(self, record: TransferRecord) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO transfers(job_id,source,destination,checksum,algorithm,status,completed_at) VALUES(?,?,?,?,?,?,?)",
                              (record.job_id, record.source, record.destination, record.destination_checksum,
                               record.checksum_algorithm, record.status, record.completed_at))


class BackupEngine:
    def __init__(self, cancel_event: threading.Event, progress_cb: Callable[[int], None]):
        self.cancel_event = cancel_event
        self.progress_cb = progress_cb

    def checksum(self, path: Path, algorithm: str, report_progress: bool = False) -> str:
        if algorithm not in {"sha256", "md5"}:
            raise ValueError(f"Unsupported checksum: {algorithm}")
        h = hashlib.new(algorithm)
        with path.open("rb") as f:
            while True:
                if self.cancel_event.is_set():
                    raise CancelledError()
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                if report_progress:
                    self.progress_cb(len(chunk))
        return h.hexdigest()

    def verified_copy(self, source: Path, destination: Path, algorithm: str) -> tuple[str, str]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
        partial.unlink(missing_ok=True)
        source_hash = hashlib.new(algorithm)
        try:
            with source.open("rb") as src, partial.open("xb") as dst:
                while True:
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    source_hash.update(chunk)
                    self.progress_cb(len(chunk))
                dst.flush()
                os.fsync(dst.fileno())
            shutil.copystat(source, partial, follow_symlinks=True)
            src_digest = source_hash.hexdigest()
            dst_digest = self.checksum(partial, algorithm, report_progress=True)
            if src_digest != dst_digest or source.stat().st_size != partial.stat().st_size:
                raise IOError("Checksum or file-size mismatch")
            os.replace(partial, destination)
            return src_digest, dst_digest
        except Exception:
            partial.unlink(missing_ok=True)
            raise


class ReportWriter:
    @staticmethod
    def _write_csv(path: Path, records: list[TransferRecord]) -> None:
        fields = list(TransferRecord.__dataclass_fields__.keys())
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader(); writer.writerows(asdict(r) for r in records)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_html(path: Path, title: str, config: JobConfig, records: list[TransferRecord], summary: dict) -> None:
        rows = "\n".join(
            f"<tr><td>{_h(r.filename)}</td><td>{_h(r.media_type)}</td><td>{_h(r.camera)}</td><td>{_h(r.status)}</td><td>{_h(r.destination)}</td><td>{_h(r.error)}</td></tr>"
            for r in records
        )
        html = f"""<!doctype html><meta charset='utf-8'><title>{_h(title)}</title>
<style>body{{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#101216;color:#e8eaf0;margin:32px}}h1{{margin:0}}.sub{{color:#9ca3af}}.cards{{display:flex;gap:12px;margin:24px 0}}.card{{background:#1b1f26;padding:16px 22px;border-radius:10px}}table{{width:100%;border-collapse:collapse;background:#171a20}}th,td{{padding:9px;border-bottom:1px solid #303640;text-align:left;vertical-align:top}}th{{color:#7dd3fc}}.ok{{color:#34d399}}</style>
<h1>{_h(title)}</h1><div class='sub'>{datetime.now().isoformat(timespec='seconds')}</div>
<div class='cards'><div class='card'>Records<br><b>{len(records)}</b></div><div class='card'>Verified<br><b class='ok'>{summary.get('verified',0)}</b></div><div class='card'>Failed<br><b>{summary.get('failed',0)}</b></div><div class='card'>Bytes<br><b>{_h(summary.get('bytes_human',''))}</b></div></div>
<p><b>Sources:</b> {_h(' | '.join(config.sources))}<br><b>Destinations:</b> {_h(', '.join(config.destinations))}<br><b>Checksum:</b> {_h(config.checksum.upper())}</p>
<table><thead><tr><th>File</th><th>Type</th><th>Device</th><th>Status</th><th>Destination</th><th>Note/Error</th></tr></thead><tbody>{rows}</tbody></table>"""
        path.write_text(html, encoding="utf-8")

    @staticmethod
    def write(report_dir: Path, job_id: str, config: JobConfig, records: list[TransferRecord], summary: dict) -> list[Path]:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        formats = set(config.report_formats or ("csv",))
        payload = {"application": APP_NAME, "version": APP_VERSION, "job_id": job_id,
                   "config": asdict(config), "summary": summary, "records": [asdict(r) for r in records]}
        written: list[Path] = []
        base = report_dir / f"MediaOrganizer_Verification_{stamp}_{job_id[:8]}"
        if "csv" in formats:
            path = base.with_suffix(".csv"); ReportWriter._write_csv(path, records); written.append(path)
        if "json" in formats:
            path = base.with_suffix(".json"); ReportWriter._write_json(path, payload); written.append(path)
        if "html" in formats:
            path = base.with_suffix(".html"); ReportWriter._write_html(path, f"{APP_NAME} Verification Report", config, records, summary); written.append(path)

        duplicates = [r for r in records if "DUPLICATE" in r.status]
        if duplicates:
            dup_dir = report_dir / "Duplicates"
            dup_dir.mkdir(parents=True, exist_ok=True)
            dup_summary = dict(summary); dup_summary["duplicate_records"] = len(duplicates)
            dup_payload = {"application": APP_NAME, "version": APP_VERSION, "job_id": job_id,
                           "report_type": "duplicates", "config": asdict(config), "summary": dup_summary,
                           "records": [asdict(r) for r in duplicates]}
            dup_base = dup_dir / f"Duplicate_Files_{stamp}_{job_id[:8]}"
            if "csv" in formats:
                path = dup_base.with_suffix(".csv"); ReportWriter._write_csv(path, duplicates); written.append(path)
            if "json" in formats:
                path = dup_base.with_suffix(".json"); ReportWriter._write_json(path, dup_payload); written.append(path)
            if "html" in formats:
                path = dup_base.with_suffix(".html"); ReportWriter._write_html(path, "Duplicate Files Report", config, duplicates, dup_summary); written.append(path)
        return written


def _h(value) -> str:
    import html
    return html.escape(str(value))


class MediaOrganizerApp(Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · {APP_VERSION}")
        self.geometry("1280x820")
        self.minsize(1050, 680)
        self.configure(bg="#0f1115")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        app_data = Path.home() / "Library" / "Application Support" / "MediaOrganizerPro" if sys.platform == "darwin" else Path.home() / ".media_organizer_pro"
        self.db = AssetDatabase(app_data / "library.sqlite3")
        self.metadata_engine = MetadataEngine(self.db)
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.records: list[TransferRecord] = []
        self.scanned_files: list[Path] = []
        self.clip_packages: list[ClipPackage] = []
        self.package_by_primary: dict[str, ClipPackage] = {}
        self.video_resolver = VideoMetadataResolver(self.metadata_engine)
        self.audio_resolver = AudioMetadataResolver()
        self.current_config: Optional[JobConfig] = None
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_work_bytes = 0
        self.transfer_started_monotonic = 0.0
        self.speed_samples: list[tuple[float, int]] = []
        self.session_id = 0
        self.last_completed_session = 0

        self.source_var = StringVar()
        self.source_paths: list[Path] = []
        self.dest1_var = StringVar()
        self.dest2_var = StringVar()
        self.operation_var = StringVar(value="copy")
        self.checksum_var = StringVar(value="sha256")
        self.group_camera_var = BooleanVar(value=True)
        self.group_date_var = BooleanVar(value=True)
        self.sidecars_var = BooleanVar(value=True)
        self.report_var = BooleanVar(value=True)
        self.duplicate_policy_var = StringVar(value="skip")
        self.report_csv_var = BooleanVar(value=True)
        self.report_json_var = BooleanVar(value=True)
        self.report_html_var = BooleanVar(value=True)
        self.date_structure_var = StringVar(value="YYYY/MM/DD")
        self.status_var = StringVar(value="READY")
        self.progress_text_var = StringVar(value="0 files · 0 B")
        self.eta_text_var = StringVar(value="")

        self._build_style()
        self._build_ui()
        self.after(100, self._drain_ui_queue)

    def tr(self, key: str) -> str:
        lang = self.language_var.get() if hasattr(self, "language_var") else "中文"
        table = {
            "中文": {
                "ingest": "素材导入",
                "source": "源素材文件夹（可多选）",
                "primary": "主备份位置",
                "secondary": "第二备份位置",
                "optional": "可选",
                "policy": "传输方式",
                "copy": "安全复制（保留源文件）",
                "copy_help": "完成校验后保留存储卡或源盘中的原文件。",
                "move": "校验后移动（所有目标通过后删除源文件）",
                "move_help": "仅在所有备份目标校验成功且记录写入后删除源文件。",
                "verification": "完整性校验",
                "checksum_help": "SHA-256 更安全；MD5 速度更快、兼容性更好。",
                "organize": "整理规则",
                "camera": "按拍摄设备型号整理照片、视频和录音",
                "date": "按拍摄日期创建文件夹",
                "sidecars": "同时复制伴随文件（XMP / XML / JSON 等）",
                "report": "生成校验报告",
                "report_formats": "报告格式",
                "report_csv": "CSV（表格分析）",
                "report_json": "JSON（完整机器可读记录）",
                "report_html": "HTML（可视化报告）",
                "start": "一键扫描并开始备份",
                "duplicate_policy": "重复文件处理",
                "duplicate_skip": "跳过完全重复文件（推荐）",
                "duplicate_keep": "保留另一份副本（自动改名）",
                "duplicate_folder": "复制到 Duplicates 文件夹",
                "duplicate_help_skip": "内容完全一致时不再复制；报告会记录已存在且校验一致的副本位置。",
                "duplicate_help_keep": "完全重复的文件仍会复制，并使用 __duplicate__校验值 自动改名，避免覆盖。",
                "duplicate_help_folder": "完全重复的文件会集中复制到当前分类目录下的 Duplicates 文件夹，便于人工复核。",
                "stop": "安全停止",
                "queue": "素材队列",
                "file": "文件",
                "type": "类型",
                "camera_col": "相机",
                "capture": "拍摄时间",
                "size": "大小",
                "status": "状态",
                "inspector": "元数据检查器",
                "log": "任务日志",
                "browse": "选择",
                "add_source": "添加来源",
                "remove_source": "删除所选",
                "clear_sources": "清空来源",
                "source_count": "已添加 {count} 个来源",
                "language": "界面语言",
                "eta_wait": "预计时间：扫描完成后计算",
                "eta": "剩余 {remaining} · 预计 {finish} 完成 · {speed}/s",
                "package": "素材包",
                "exif_ok": "ExifTool 已启用：可读取完整相机元数据",
                "exif_fallback": "未检测到 ExifTool：正在使用 macOS 元数据兼容模式",
            },
            "English": {
                "ingest": "INGEST SETUP",
                "source": "Source media folders (multiple allowed)",
                "primary": "Primary backup destination",
                "secondary": "Secondary backup destination",
                "optional": "optional",
                "policy": "TRANSFER MODE",
                "copy": "Safe Copy (keep source files)",
                "copy_help": "Keeps every original on the source after checksum verification.",
                "move": "Verified Move (delete source after all targets pass)",
                "move_help": "Deletes a source file only after every selected target verifies and the record is committed.",
                "verification": "INTEGRITY CHECK",
                "checksum_help": "SHA-256 is safer; MD5 is faster and broadly compatible.",
                "organize": "ORGANIZATION",
                "camera": "Organize photos, video and audio by recording device",
                "date": "Create folders by capture date",
                "sidecars": "Copy sidecar files (XMP / XML / JSON, etc.)",
                "report": "Create verification reports",
                "report_formats": "Report formats",
                "report_csv": "CSV (spreadsheet analysis)",
                "report_json": "JSON (complete machine-readable record)",
                "report_html": "HTML (visual report)",
                "start": "Scan & Start Backup",
                "duplicate_policy": "DUPLICATE HANDLING",
                "duplicate_skip": "Skip exact duplicates (recommended)",
                "duplicate_keep": "Keep another copy (auto-rename)",
                "duplicate_folder": "Copy into a Duplicates folder",
                "duplicate_help_skip": "No additional copy is created when contents match; the verified existing path is recorded.",
                "duplicate_help_keep": "An exact duplicate is copied with a deterministic __duplicate__ checksum suffix, without overwriting anything.",
                "duplicate_help_folder": "Exact duplicates are copied into a Duplicates subfolder for later manual review.",
                "stop": "Stop Safely",
                "queue": "Media Queue",
                "file": "File",
                "type": "Type",
                "camera_col": "Camera",
                "capture": "Capture Date",
                "size": "Size",
                "status": "Status",
                "inspector": "Metadata Inspector",
                "log": "Activity Log",
                "browse": "Choose",
                "add_source": "Add Source",
                "remove_source": "Remove Selected",
                "clear_sources": "Clear Sources",
                "source_count": "{count} sources added",
                "language": "Language",
                "exif_ok": "ExifTool enabled: full camera metadata available",
                "exif_fallback": "ExifTool not detected: using macOS metadata fallback",
            },
        }
        return table.get(lang, table["中文"]).get(key, key)

    def _build_style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        self.option_add("*Font", ("Helvetica Neue", 12))
        self.option_add("*TCombobox*Listbox.background", "#1c2129")
        self.option_add("*TCombobox*Listbox.foreground", "#edf1f5")
        self.option_add("*TCombobox*Listbox.selectBackground", "#0a84ff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        s.configure(".", background="#11151b", foreground="#e8edf3", bordercolor="#343c48", lightcolor="#343c48", darkcolor="#343c48")
        s.configure("TFrame", background="#11151b")
        s.configure("Sidebar.TFrame", background="#0d1117")
        s.configure("Card.TFrame", background="#171c24")
        s.configure("TLabel", background="#11151b", foreground="#dce3eb")
        s.configure("Sidebar.TLabel", background="#0d1117", foreground="#dce3eb")
        s.configure("Header.TLabel", font=("Helvetica Neue", 22, "bold"), foreground="#f7f9fb", background="#11151b")
        s.configure("Sub.TLabel", font=("Helvetica Neue", 10), foreground="#8f9baa", background="#11151b")
        s.configure("Section.TLabel", font=("Helvetica Neue", 11, "bold"), foreground="#96a2b1", background="#0d1117")
        s.configure("Hint.TLabel", font=("Helvetica Neue", 10), foreground="#7f8b99", background="#0d1117")
        s.configure("Status.TLabel", font=("Helvetica Neue", 10, "bold"), foreground="#66d9ff", background="#11151b")
        s.configure("TEntry", fieldbackground="#1a2029", foreground="#eef2f6", insertcolor="#ffffff", bordercolor="#394352", padding=7)
        s.configure("TCombobox", fieldbackground="#1a2029", background="#1a2029", foreground="#eef2f6", arrowcolor="#dce3eb", bordercolor="#394352", padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", "#1a2029"), ("disabled", "#151a21")], foreground=[("readonly", "#eef2f6"), ("disabled", "#6f7884")], selectbackground=[("readonly", "#1a2029")], selectforeground=[("readonly", "#eef2f6")])
        s.configure("TButton", padding=(14, 10), background="#29313d", foreground="#f7f9fb", borderwidth=0, font=("Helvetica Neue", 11, "bold"))
        s.map("TButton", background=[("active", "#394554"), ("pressed", "#202833"), ("disabled", "#1a2028")], foreground=[("disabled", "#687382")])
        s.configure("Browse.TButton", padding=(10, 7), background="#222a35", font=("Helvetica Neue", 10, "bold"))
        s.configure("Primary.TButton", padding=(14, 11), background="#0a84ff", foreground="white", font=("Helvetica Neue", 11, "bold"))
        s.map("Primary.TButton", background=[("active", "#3198ff"), ("pressed", "#006ed6"), ("disabled", "#23445f")], foreground=[("disabled", "#8fa6b8")])
        s.configure("Danger.TButton", padding=(14, 11), background="#8b2b32", foreground="white", font=("Helvetica Neue", 11, "bold"))
        s.map("Danger.TButton", background=[("active", "#a63a43"), ("disabled", "#39252a")], foreground=[("disabled", "#8b7478")])
        s.configure("Treeview", background="#141920", fieldbackground="#141920", foreground="#dfe6ee", rowheight=30, borderwidth=0)
        s.configure("Treeview.Heading", background="#242b35", foreground="#aeb9c6", relief="flat", font=("Helvetica Neue", 10, "bold"))
        s.map("Treeview", background=[("selected", "#164f7a")], foreground=[("selected", "#ffffff")])
        s.configure("TProgressbar", troughcolor="#252c35", background="#2ecb91", thickness=8)
        s.configure("TNotebook", background="#11151b", borderwidth=0)
        s.configure("TNotebook.Tab", background="#202630", foreground="#aeb7c2", padding=(18, 10), font=("Helvetica Neue", 11, "bold"))
        s.map("TNotebook.Tab", background=[("selected", "#303946"), ("active", "#29313b")], foreground=[("selected", "#ffffff")])

    def _dark_check(self, parent, textvariable: StringVar, variable: BooleanVar):
        return tk.Checkbutton(parent, textvariable=textvariable, variable=variable, anchor="w", justify="left",
                              bg="#0d1117", fg="#e7ebf0", activebackground="#0d1117", activeforeground="#ffffff",
                              selectcolor="#0a84ff", highlightthickness=0, bd=0, relief="flat",
                              font=("Helvetica Neue", 12), padx=0, pady=4)

    def _dark_radio(self, parent, textvariable: StringVar, variable: StringVar, value: str):
        return tk.Radiobutton(parent, textvariable=textvariable, variable=variable, value=value, anchor="w", justify="left",
                              bg="#0d1117", fg="#e7ebf0", activebackground="#0d1117", activeforeground="#ffffff",
                              selectcolor="#0a84ff", highlightthickness=0, bd=0, relief="flat",
                              font=("Helvetica Neue", 12), padx=0, pady=3)

    def _build_ui(self):
        top = ttk.Frame(self, padding=(22, 14))
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, style="Header.TLabel").pack(side="left")
        right = ttk.Frame(top)
        right.pack(side="right")
        self.language_var = StringVar(value="中文")
        self.language_label = ttk.Label(right, text="界面语言", style="TLabel")
        self.language_label.pack(side="left", padx=(0, 7))
        self.language_combo = ttk.Combobox(right, textvariable=self.language_var, values=("中文", "English"), state="readonly", width=9)
        self.language_combo.pack(side="left", padx=(0, 16))
        self.language_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_texts())
        self.status_label = ttk.Label(right, text=self.status_var.get(), style="Status.TLabel")
        self.status_label.pack(side="left")

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        # The sidebar uses a scrollable settings area plus a fixed action footer.
        # This keeps Start/Stop visible even when the window is not fullscreen.
        side_shell = ttk.Frame(body, style="Sidebar.TFrame", width=370)
        main = ttk.Frame(body, padding=(14, 0, 0, 0))
        body.add(side_shell, weight=0); body.add(main, weight=1)

        action_footer = ttk.Frame(side_shell, style="Sidebar.TFrame", padding=(18, 10, 18, 16))
        action_footer.pack(side="bottom", fill="x")
        ttk.Separator(action_footer).pack(fill="x", pady=(0, 12))

        sidebar_host = ttk.Frame(side_shell, style="Sidebar.TFrame")
        sidebar_host.pack(side="top", fill="both", expand=True)
        sidebar_canvas = tk.Canvas(sidebar_host, bg="#0d1117", highlightthickness=0, bd=0)
        sidebar_scroll = ttk.Scrollbar(sidebar_host, orient="vertical", command=sidebar_canvas.yview)
        sidebar_canvas.configure(yscrollcommand=sidebar_scroll.set)
        sidebar_scroll.pack(side="right", fill="y")
        sidebar_canvas.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(sidebar_canvas, style="Sidebar.TFrame", padding=18)
        side_window = sidebar_canvas.create_window((0, 0), window=side, anchor="nw")
        side.bind("<Configure>", lambda _e: sidebar_canvas.configure(scrollregion=sidebar_canvas.bbox("all")))
        sidebar_canvas.bind("<Configure>", lambda e: sidebar_canvas.itemconfigure(side_window, width=e.width))
        sidebar_canvas.bind_all("<MouseWheel>", lambda e: sidebar_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        self.ingest_label = ttk.Label(side, style="Section.TLabel")
        self.ingest_label.pack(anchor="w", pady=(0, 9))
        self.source_title = StringVar(); self.primary_title = StringVar(); self.secondary_title = StringVar()
        self._source_picker(side)
        self._path_row(side, self.primary_title, self.dest1_var, lambda: self.choose_dest(self.dest1_var))
        self._path_row(side, self.secondary_title, self.dest2_var, lambda: self.choose_dest(self.dest2_var))

        ttk.Separator(side).pack(fill="x", pady=15)
        self.policy_label = ttk.Label(side, style="Section.TLabel"); self.policy_label.pack(anchor="w", pady=(0, 6))
        self.copy_text = StringVar(); self.move_text = StringVar(); self.copy_help_text = StringVar(); self.move_help_text = StringVar()
        self.copy_radio = self._dark_radio(side, self.copy_text, self.operation_var, "copy"); self.copy_radio.pack(fill="x", anchor="w")
        ttk.Label(side, textvariable=self.copy_help_text, style="Hint.TLabel", wraplength=305).pack(anchor="w", padx=(24, 0), pady=(0, 5))
        self.move_radio = self._dark_radio(side, self.move_text, self.operation_var, "move"); self.move_radio.pack(fill="x", anchor="w")
        ttk.Label(side, textvariable=self.move_help_text, style="Hint.TLabel", wraplength=305).pack(anchor="w", padx=(24, 0), pady=(0, 5))

        ttk.Separator(side).pack(fill="x", pady=14)
        self.verification_label = ttk.Label(side, style="Section.TLabel"); self.verification_label.pack(anchor="w", pady=(0, 6))
        self.checksum_combo = ttk.Combobox(side, textvariable=self.checksum_var, values=("sha256", "md5"), state="readonly", width=18)
        self.checksum_combo.pack(fill="x", anchor="w")
        self.checksum_help_text = StringVar()
        ttk.Label(side, textvariable=self.checksum_help_text, style="Hint.TLabel", wraplength=305).pack(anchor="w", pady=(5, 0))

        ttk.Separator(side).pack(fill="x", pady=14)
        self.organize_label = ttk.Label(side, style="Section.TLabel"); self.organize_label.pack(anchor="w", pady=(0, 4))
        self.camera_text = StringVar(); self.date_text = StringVar(); self.sidecars_text = StringVar(); self.report_text = StringVar()
        self.camera_check = self._dark_check(side, self.camera_text, self.group_camera_var); self.camera_check.pack(fill="x", anchor="w")
        self.date_check = self._dark_check(side, self.date_text, self.group_date_var); self.date_check.pack(fill="x", anchor="w")
        self.date_combo = ttk.Combobox(side, textvariable=self.date_structure_var, values=("YYYY/MM/DD", "YYYY/MMDD", "YYYYMMDD"), state="readonly", width=18)
        self.date_combo.pack(fill="x", anchor="w", pady=(3, 6))
        self.sidecars_check = self._dark_check(side, self.sidecars_text, self.sidecars_var); self.sidecars_check.pack(fill="x", anchor="w")
        self.report_check = self._dark_check(side, self.report_text, self.report_var); self.report_check.pack(fill="x", anchor="w")
        self.report_formats_label = ttk.Label(side, style="Hint.TLabel"); self.report_formats_label.pack(anchor="w", pady=(5, 1))
        self.report_csv_text = StringVar(); self.report_json_text = StringVar(); self.report_html_text = StringVar()
        report_row = ttk.Frame(side, style="Sidebar.TFrame")
        report_row.pack(fill="x", anchor="w")
        self.report_csv_check = self._dark_check(report_row, self.report_csv_text, self.report_csv_var); self.report_csv_check.pack(anchor="w")
        self.report_json_check = self._dark_check(report_row, self.report_json_text, self.report_json_var); self.report_json_check.pack(anchor="w")
        self.report_html_check = self._dark_check(report_row, self.report_html_text, self.report_html_var); self.report_html_check.pack(anchor="w")

        ttk.Separator(side).pack(fill="x", pady=14)
        self.duplicate_label = ttk.Label(side, style="Section.TLabel"); self.duplicate_label.pack(anchor="w", pady=(0, 6))
        self.duplicate_text = StringVar(); self.duplicate_help_text = StringVar()
        self.duplicate_combo = ttk.Combobox(side, textvariable=self.duplicate_text, state="readonly", width=28)
        self.duplicate_combo.pack(fill="x", anchor="w")
        self.duplicate_combo.bind("<<ComboboxSelected>>", self._duplicate_selection_changed)
        ttk.Label(side, textvariable=self.duplicate_help_text, style="Hint.TLabel", wraplength=305).pack(anchor="w", pady=(5, 0))

        self.start_btn = ttk.Button(action_footer, style="Primary.TButton", command=self.start_one_click)
        self.start_btn.pack(fill="x", pady=(0, 8))
        self.stop_btn = ttk.Button(action_footer, style="Danger.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(fill="x")
        self.exif_label = ttk.Label(action_footer, style="Hint.TLabel", wraplength=315)
        self.exif_label.pack(anchor="w", pady=(10, 0))

        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 10))
        self.queue_label = ttk.Label(header, font=("Helvetica Neue", 16, "bold")); self.queue_label.pack(side="left")
        ttk.Label(header, textvariable=self.progress_text_var, style="Sub.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(main, textvariable=self.eta_text_var, style="Sub.TLabel").pack(fill="x", pady=(0, 10))

        columns = ("type", "camera", "date", "size", "status")
        self.tree = ttk.Treeview(main, columns=columns, show="tree headings", selectmode="browse")
        self.tree.column("#0", width=300, minwidth=180)
        for cid, width in (("type", 90), ("camera", 190), ("date", 150), ("size", 95), ("status", 130)):
            self.tree.column(cid, width=width, minwidth=70)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected_metadata)

        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=False, pady=(12, 0))
        inspect_frame = ttk.Frame(self.tabs, padding=10); log_frame = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(inspect_frame, text="") ; self.tabs.add(log_frame, text="")
        self.inspector = ScrolledText(inspect_frame, height=9, bg="#10151c", fg="#dce3eb", insertbackground="white", relief="flat", font=("Menlo", 11), padx=10, pady=8)
        self.inspector.pack(fill="both", expand=True); self.inspector.configure(state="disabled")
        self.log_text = ScrolledText(log_frame, height=9, bg="#0b0f14", fg="#bdc7d2", insertbackground="white", relief="flat", font=("Menlo", 10), padx=10, pady=8)
        self.log_text.pack(fill="both", expand=True); self.log_text.configure(state="disabled")
        self._update_texts()

    def _update_texts(self):
        self.language_label.configure(text=self.tr("language"))
        self.ingest_label.configure(text=self.tr("ingest"))
        self.source_title.set(self.tr("source"))
        self.primary_title.set(self.tr("primary"))
        self.secondary_title.set(f"{self.tr('secondary')} ({self.tr('optional')})")
        self.policy_label.configure(text=self.tr("policy"))
        self.copy_text.set(self.tr("copy")); self.copy_help_text.set(self.tr("copy_help"))
        self.move_text.set(self.tr("move")); self.move_help_text.set(self.tr("move_help"))
        self.verification_label.configure(text=self.tr("verification")); self.checksum_help_text.set(self.tr("checksum_help"))
        self.organize_label.configure(text=self.tr("organize"))
        self.camera_text.set(self.tr("camera")); self.date_text.set(self.tr("date")); self.sidecars_text.set(self.tr("sidecars")); self.report_text.set(self.tr("report"))
        self.report_formats_label.configure(text=self.tr("report_formats"))
        self.report_csv_text.set(self.tr("report_csv")); self.report_json_text.set(self.tr("report_json")); self.report_html_text.set(self.tr("report_html"))
        self.start_btn.configure(text=self.tr("start")); self.stop_btn.configure(text=self.tr("stop"))
        self.duplicate_label.configure(text=self.tr("duplicate_policy"))
        duplicate_values = (
            self.tr("duplicate_skip"),
            self.tr("duplicate_keep"),
            self.tr("duplicate_folder"),
        )
        self.duplicate_combo.configure(values=duplicate_values)
        code_to_text = {
            "skip": duplicate_values[0],
            "keep": duplicate_values[1],
            "folder": duplicate_values[2],
        }
        self.duplicate_text.set(code_to_text.get(self.duplicate_policy_var.get(), duplicate_values[0]))
        self._refresh_duplicate_help()
        self.exif_label.configure(text=self.tr("exif_ok") if self.metadata_engine.exiftool else self.tr("exif_fallback"))
        self.queue_label.configure(text=self.tr("queue"))
        if not (self.worker and self.worker.is_alive()): self.eta_text_var.set(self.tr("eta_wait"))
        self.tree.heading("#0", text=self.tr("file"))
        self.tree.heading("type", text=self.tr("type")); self.tree.heading("camera", text=self.tr("camera_col")); self.tree.heading("date", text=self.tr("capture")); self.tree.heading("size", text=self.tr("size")); self.tree.heading("status", text=self.tr("status"))
        self.tabs.tab(0, text=self.tr("inspector")); self.tabs.tab(1, text=self.tr("log"))
        for btn in getattr(self, "browse_buttons", []): btn.configure(text=self.tr("browse"))
        if hasattr(self, "add_source_btn"):
            self.add_source_btn.configure(text=self.tr("add_source"))
            self.remove_source_btn.configure(text=self.tr("remove_source"))
            self.clear_sources_btn.configure(text=self.tr("clear_sources"))
            self._refresh_source_list()


    def _duplicate_selection_changed(self, _event=None):
        text_to_code = {
            self.tr("duplicate_skip"): "skip",
            self.tr("duplicate_keep"): "keep",
            self.tr("duplicate_folder"): "folder",
        }
        self.duplicate_policy_var.set(text_to_code.get(self.duplicate_text.get(), "skip"))
        self._refresh_duplicate_help()

    def _refresh_duplicate_help(self):
        key = {
            "skip": "duplicate_help_skip",
            "keep": "duplicate_help_keep",
            "folder": "duplicate_help_folder",
        }.get(self.duplicate_policy_var.get(), "duplicate_help_skip")
        self.duplicate_help_text.set(self.tr(key))

    def _source_picker(self, parent):
        ttk.Label(parent, textvariable=self.source_title, style="Hint.TLabel").pack(anchor="w", pady=(7, 3))
        shell = ttk.Frame(parent, style="Sidebar.TFrame")
        shell.pack(fill="x")
        self.source_listbox = tk.Listbox(
            shell, height=5, selectmode="extended", exportselection=False,
            bg="#10151c", fg="#e6edf3", selectbackground="#0a84ff",
            selectforeground="#ffffff", highlightthickness=1,
            highlightbackground="#30363d", highlightcolor="#0a84ff",
            relief="flat", borderwidth=0, font=("Helvetica Neue", 11)
        )
        source_scroll = ttk.Scrollbar(shell, orient="vertical", command=self.source_listbox.yview)
        self.source_listbox.configure(yscrollcommand=source_scroll.set)
        self.source_listbox.pack(side="left", fill="x", expand=True)
        source_scroll.pack(side="right", fill="y")

        controls = ttk.Frame(parent, style="Sidebar.TFrame")
        controls.pack(fill="x", pady=(6, 0))
        self.add_source_btn = ttk.Button(controls, style="Browse.TButton", command=self.choose_source)
        self.add_source_btn.pack(side="left", fill="x", expand=True)
        self.remove_source_btn = ttk.Button(controls, style="Browse.TButton", command=self.remove_selected_sources)
        self.remove_source_btn.pack(side="left", fill="x", expand=True, padx=5)
        self.clear_sources_btn = ttk.Button(controls, style="Browse.TButton", command=self.clear_sources)
        self.clear_sources_btn.pack(side="left", fill="x", expand=True)
        self.source_count_label = ttk.Label(parent, style="Hint.TLabel")
        self.source_count_label.pack(anchor="w", pady=(4, 0))
        self._refresh_source_list()

    def _path_row(self, parent, title_var, variable, command):
        ttk.Label(parent, textvariable=title_var, style="Hint.TLabel").pack(anchor="w", pady=(7, 3))
        row = ttk.Frame(parent, style="Sidebar.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        btn = ttk.Button(row, width=6, style="Browse.TButton", command=command)
        btn.pack(side="left", padx=(6, 0))
        if not hasattr(self, "browse_buttons"): self.browse_buttons = []
        self.browse_buttons.append(btn)

    def choose_source(self):
        """Add one or more folders without replacing existing sources."""
        title = "添加一个或多个源素材文件夹" if self.language_var.get() == "中文" else "Add one or more source media folders"
        selected: list[str] = []
        if sys.platform == "darwin":
            escaped = title.replace('\"', '\\"')
            script = (f'set chosenFolders to choose folder with prompt "{escaped}" with multiple selections allowed\n'
                      'set outputText to ""\n'
                      'repeat with oneFolder in chosenFolders\n'
                      'set outputText to outputText & POSIX path of oneFolder & linefeed\n'
                      'end repeat\n'
                      'return outputText')
            try:
                proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
                if proc.returncode == 0:
                    selected = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
                elif "User canceled" not in proc.stderr:
                    self.log(f"Folder picker error: {proc.stderr.strip()}")
            except Exception as exc:
                self.log(f"Multiple folder picker unavailable: {exc}")
        else:
            p = filedialog.askdirectory(title=title)
            if p:
                selected = [p]
        if selected:
            self._add_source_paths(selected)

    def _add_source_paths(self, paths: Iterable[str | Path]):
        self._set_source_paths([*self.source_paths, *paths])

    def remove_selected_sources(self):
        if not hasattr(self, "source_listbox"):
            return
        selected = set(self.source_listbox.curselection())
        if not selected:
            return
        remaining = [p for i, p in enumerate(self.source_paths) if i not in selected]
        self._set_source_paths(remaining)

    def clear_sources(self):
        self._set_source_paths([])

    def _refresh_source_list(self):
        if hasattr(self, "source_listbox"):
            self.source_listbox.delete(0, "end")
            for p in self.source_paths:
                self.source_listbox.insert("end", str(p))
        if hasattr(self, "source_count_label"):
            self.source_count_label.configure(text=self.tr("source_count").format(count=len(self.source_paths)))

    def _set_source_paths(self, paths: Iterable[str | Path]):
        resolved: list[Path] = []
        for raw in paths:
            p = Path(raw).expanduser().resolve()
            if p.is_dir() and p not in resolved:
                resolved.append(p)
        roots: list[Path] = []
        for candidate in sorted(resolved, key=lambda x: len(x.parts)):
            if not any(candidate == root or root in candidate.parents for root in roots):
                roots.append(candidate)
        self.source_paths = roots
        if not roots:
            self.source_var.set("")
        elif len(roots) == 1:
            self.source_var.set(str(roots[0]))
        else:
            self.source_var.set(" | ".join(map(str, roots)))
        self._refresh_source_list()

    def choose_dest(self, var):
        title = "选择备份目标文件夹" if self.language_var.get() == "中文" else "Select backup destination"
        p = filedialog.askdirectory(title=title)
        if p: var.set(p)

    def log(self, message: str):
        self.ui_queue.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {message}"))

    def set_status(self, value: str):
        self.ui_queue.put(("status", value))

    def start_one_click(self):
        if self.worker and self.worker.is_alive():
            return
        # A completed Thread object must not leak state into the next ingest.
        if self.worker is not None:
            try:
                self.worker.join(timeout=0)
            except RuntimeError:
                pass
            self.worker = None
        # Remove stale UI messages left by the previous session before resetting widgets.
        try:
            while True:
                self.ui_queue.get_nowait()
        except queue.Empty:
            pass
        self.session_id += 1
        session_id = self.session_id
        try:
            config = self._snapshot_config()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        if config.operation == "move":
            prompt = (
                "校验后移动会在每个文件的所有备份目标均已校验通过，或确认目标中已有完全相同副本后，才删除对应源文件。是否继续？"
                if self.language_var.get() == "中文" else
                "Verified Move deletes a source only after every destination has either verified a new copy or confirmed an exact existing duplicate. Continue?"
            )
            if not messagebox.askyesno(APP_NAME, prompt):
                return
        self.current_config = config
        self.cancel_event.clear()
        self.done_bytes = 0
        self.records.clear()
        self.scanned_files.clear()
        self.clip_packages.clear()
        self.package_by_primary.clear()
        self.total_bytes = 0
        self.total_work_bytes = 0
        self.transfer_started_monotonic = 0.0
        self.speed_samples.clear()
        self.eta_text_var.set(self.tr("eta_wait"))
        self.progress["value"] = 0
        self.progress_text_var.set("0% · 0 B")
        self.tree.delete(*self.tree.get_children())
        self._set_running(True)
        self.worker = threading.Thread(target=self._one_click_worker, args=(config, session_id), daemon=False)
        self.worker.start()

    def _one_click_worker(self, config: JobConfig, session_id: int):
        try:
            sources = [Path(value) for value in config.sources]
            self.set_status("SCANNING")
            files = []
            seen_files: set[str] = set()
            for source in sources:
                self.log(f"Scanning source: {source}")
                for root, dirs, names in os.walk(source, followlinks=False):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    for name in names:
                        p = Path(root) / name
                        key = os.path.normcase(os.path.realpath(p))
                        if key not in seen_files and not name.startswith(".") and name not in {"Thumbs.db", "desktop.ini"} and PARTIAL_SUFFIX not in name:
                            files.append(p)
                            seen_files.add(key)
            self.clip_packages = self._build_clip_packages(files, include_sidecars=config.include_sidecars)
            self.package_by_primary = {str(pkg.primary): pkg for pkg in self.clip_packages}
            self.scanned_files = [pkg.primary for pkg in self.clip_packages]
            self.total_bytes = sum(pkg.total_size for pkg in self.clip_packages)
            self.total_work_bytes = self.total_bytes * max(1, len(config.destinations)) * 2
            for idx, pkg in enumerate(self.clip_packages, 1):
                if self.cancel_event.is_set(): raise CancelledError()
                self.ui_queue.put(("package", pkg, idx, len(self.clip_packages)))
            self.log(f"Scan complete: {len(self.clip_packages)} clip packages / standalone assets, {format_bytes(self.total_bytes)}")
            if not self.scanned_files:
                self.set_status("NO MEDIA FOUND")
                self.log("No supported media files were found.")
                return
            self._transfer_worker(config, own_running_state=False, session_id=session_id)
        except CancelledError:
            self.log("Task cancelled safely; current partial file removed.")
            self.set_status("CANCELLED")
        except Exception as e:
            self.log(f"Task failed: {e}\n{traceback.format_exc()}")
            self.set_status("ERROR")
        finally:
            self.ui_queue.put(("session_finished", session_id))

    def _transfer_worker(self, config: JobConfig, own_running_state: bool = True, session_id: int | None = None):
        job_id = str(uuid.uuid4()); failed = 0; skipped = 0; verified = 0
        engine = BackupEngine(self.cancel_event, self._advance_bytes)
        try:
            self.set_status("INGESTING")
            hash_cache: dict[str, Path] = {}
            # Build a whole-destination size index once per job. This allows exact
            # duplicates to be found even when organization/date/camera settings
            # changed and the expected destination path is different.
            self.destination_indexes = {}
            self.destination_hash_cache = {}
            for dest_root_str in config.destinations:
                dest_root = Path(dest_root_str)
                self.set_status("INDEXING DESTINATION")
                self.destination_indexes[str(dest_root.resolve())] = self._build_destination_index(dest_root)
            self.transfer_started_monotonic = time.monotonic()
            self.speed_samples = [(self.transfer_started_monotonic, 0)]
            for index, source in enumerate(self.scanned_files, 1):
                if self.cancel_event.is_set(): raise CancelledError()
                item_id = str(source)
                try:
                    source_stat = source.stat()
                except OSError as exc:
                    skipped += 1
                    self.log(f"Skipping source unavailable: {source} ({exc})")
                    self.ui_queue.put(("item_status", item_id, "Skipped — source unavailable"))
                    continue
                package = self.package_by_primary.get(str(source)) or ClipPackage(str(source), source, [source], self._media_type(source), self.metadata_engine.read(source), "standalone")
                self.ui_queue.put(("item_status", item_id, "Processing"))
                meta = package.metadata
                media_type = package.media_type
                capture_dt = parse_exif_date(meta.capture_date) or datetime.fromtimestamp(source_stat.st_mtime)
                source_digest_for_job = ""
                all_dest_ok = True
                records_for_source = []
                for dest_root_str in config.destinations:
                    dest_root = Path(dest_root_str)
                    rel_dir = self._destination_dir(media_type, meta, capture_dt, config)
                    dest_dir = dest_root / rel_dir
                    package_dest_ok = True
                    for member in package.members:
                        try:
                            member_size = member.stat().st_size
                        except OSError as exc:
                            skipped += 1
                            package_dest_ok = False
                            all_dest_ok = False
                            destination = dest_dir / member.name
                            started = datetime.now().isoformat(timespec="seconds")
                            error = f"Source member unavailable: {exc}"
                            rec = TransferRecord(job_id, str(member), str(destination), str(destination.relative_to(dest_root)), member.name,
                                                 media_type if member == package.primary else f"{media_type} package member", 0,
                                                 meta.camera, meta.capture_date, config.checksum, "", "", "SKIPPED_MISSING", error,
                                                 started, datetime.now().isoformat(timespec="seconds"))
                            records_for_source.append(rec); self.records.append(rec); self.db.add_transfer(rec)
                            self.log(f"Skipping unavailable package member: {member} ({exc})")
                            continue
                        destination, collision_action, existing_hash = self._resolve_collision(
                            dest_root, dest_dir, member.name, member, config.checksum, engine, capture_dt, config.duplicate_policy
                        )
                        self.set_status("INGESTING")
                        started = datetime.now().isoformat(timespec="seconds")
                        try:
                            if collision_action == "duplicate":
                                src_hash = existing_hash; dst_hash = existing_hash
                                status = "DUPLICATE_SKIPPED"; error = "Exact content already exists; no additional copy created."
                            else:
                                src_hash, dst_hash = engine.verified_copy(member, destination, config.checksum)
                                self._index_destination_file(dest_root, destination)
                                if collision_action in ("duplicate_keep", "duplicate_folder"):
                                    status = "VERIFIED_DUPLICATE_COPY"
                                    error = "Exact duplicate retained according to duplicate policy."
                                else:
                                    status = "VERIFIED_RENAMED" if collision_action == "renamed" else "VERIFIED"
                                    error = "Renamed because the original filename was already used by different content." if collision_action == "renamed" else ""
                            verified += 1
                        except CancelledError:
                            raise
                        except Exception as e:
                            src_hash = ""; dst_hash = ""; status = "FAILED"; error = str(e)
                            package_dest_ok = False; all_dest_ok = False; failed += 1
                        rec = TransferRecord(job_id, str(member), str(destination), str(destination.relative_to(dest_root)), member.name,
                                             media_type if member == package.primary else f"{media_type} package member", member_size,
                                             meta.camera, meta.capture_date, config.checksum, src_hash, dst_hash, status, error,
                                             started, datetime.now().isoformat(timespec="seconds"))
                        records_for_source.append(rec); self.records.append(rec); self.db.add_transfer(rec)
                    all_dest_ok = all_dest_ok and package_dest_ok
                duplicate_only = bool(records_for_source) and all(r.status == "DUPLICATE_SKIPPED" for r in records_for_source)
                renamed_any = any(r.status == "VERIFIED_RENAMED" for r in records_for_source)
                if all_dest_ok and config.operation == "move":
                    # Report data is already committed to SQLite before deletion. Final external report is written after job.
                    for member in package.members:
                        member.unlink(missing_ok=True)
                    self.log(("Exact duplicate package confirmed; sources removed after all targets matched: " if duplicate_only else "Verified move completed for package: ") + source.name)
                elif all_dest_ok:
                    if duplicate_only:
                        self.log(f"Exact duplicate skipped: {source.name}")
                    elif renamed_any:
                        self.log(f"Name collision resolved with metadata/hash suffix: {source.name}")
                    else:
                        self.log(f"Verified copy completed: {source.name}")
                else:
                    self.log(f"Failed or incomplete: {source.name}; source retained")
                self.ui_queue.put(("item_status", item_id, "Verified" if all_dest_ok else "Failed"))
            summary = {"files": sum(len(p.members) for p in self.clip_packages), "packages": len(self.clip_packages), "verified": verified, "failed": failed, "skipped": skipped,
                       "bytes": self.total_bytes, "bytes_human": format_bytes(self.total_bytes)}
            report_paths = []
            if config.generate_report:
                report_root = Path(config.destinations[0]) / "MediaOrganizer_Reports"
                report_paths = ReportWriter.write(report_root, job_id, config, self.records, summary)
            self.log(f"Job complete. Verified copies: {verified}; failed: {failed}; skipped unavailable: {skipped}")
            if report_paths: self.log("Reports: " + ", ".join(str(p) for p in report_paths))
            self.set_status("COMPLETE" if failed == 0 and skipped == 0 else "COMPLETED WITH WARNINGS" if failed == 0 else "COMPLETED WITH ERRORS")
            self.ui_queue.put(("complete", failed, report_paths, skipped))
        except CancelledError:
            self.log("Stop requested. Current partial file removed; source files retained unless already fully verified and moved.")
            self.set_status("STOPPED SAFELY")
        except Exception as e:
            self.log(f"Fatal error: {e}\n{traceback.format_exc()}"); self.set_status("ERROR")
        finally:
            if own_running_state:
                self.ui_queue.put(("session_finished", session_id or self.session_id))

    @staticmethod
    def _normalized_clip_key(path: Path) -> str:
        stem = path.stem.lower()
        # Common metadata/proxy role suffixes, while preserving the actual clip number.
        stem = re.sub(r"(?:[_\-.](?:proxy|audio|lrf|lrv|low|thumb))$", "", stem)
        stem = re.sub(r"(?:m\d{2}|s\d{2})$", "", stem) if path.suffix.lower() in {".xml", ".xmp"} else stem
        return stem.rstrip("_-.")

    def _xml_references(self, xml_path: Path, candidate: Path) -> bool:
        try:
            text = xml_path.read_text(encoding="utf-8", errors="ignore").lower()
            return candidate.name.lower() in text or candidate.stem.lower() in text
        except Exception:
            return False

    def _build_clip_packages(self, files: list[Path], include_sidecars: bool = True) -> list[ClipPackage]:
        available_files: list[Path] = []
        for path in files:
            try:
                if path.is_file():
                    available_files.append(path)
                else:
                    self.log(f"Skipping source unavailable during scan: {path}")
            except OSError as exc:
                self.log(f"Skipping source unavailable during scan: {path} ({exc})")
        files = available_files
        by_dir: dict[Path, list[Path]] = {}
        for p in files: by_dir.setdefault(p.parent, []).append(p)
        used: set[Path] = set(); packages: list[ClipPackage] = []
        # Videos are authoritative primaries. Their audio/proxy/metadata companions stay adjacent.
        for primary in sorted((p for p in files if p.suffix.lower() in VIDEO_EXTS), key=lambda x: str(x).lower()):
            if primary in used: continue
            key = self._normalized_clip_key(primary)
            members = [primary]
            for sibling in by_dir.get(primary.parent, []):
                if sibling == primary or sibling in used: continue
                ext = sibling.suffix.lower()
                if ext not in (AUDIO_EXTS | SIDECAR_EXTS): continue
                if ext in SIDECAR_EXTS and not include_sidecars: continue
                same_key = self._normalized_clip_key(sibling) == key
                explicit_xml = ext in {".xml", ".xmp"} and self._xml_references(sibling, primary)
                if same_key or explicit_xml:
                    members.append(sibling)
            used.update(members)
            base = self.metadata_engine.read(primary)
            meta, confidence = self.video_resolver.resolve(primary, members, base)
            association = "filename/XML association" if len(members) > 1 else "standalone video"
            packages.append(ClipPackage(str(uuid.uuid4()), primary, sorted(members, key=lambda p: (p != primary, p.suffix.lower(), p.name.lower())), "Videos", meta, association, confidence))
        # Remaining photos and audio are standalone assets. Unassociated sidecars are preserved separately.
        for p in sorted(files, key=lambda x: str(x).lower()):
            if p in used: continue
            ext = p.suffix.lower()
            if ext in PHOTO_EXTS:
                meta = self.metadata_engine.read(p)
                packages.append(ClipPackage(str(uuid.uuid4()), p, [p], "Photos", meta, "standalone photo", "confirmed" if meta.camera != UNKNOWN_CAMERA else "unidentified"))
                used.add(p)
            elif ext in AUDIO_EXTS:
                base = self.metadata_engine.read(p)
                meta, confidence = self.audio_resolver.resolve(p, base)
                packages.append(ClipPackage(str(uuid.uuid4()), p, [p], "Audio", meta, "standalone audio", confidence)); used.add(p)
            elif ext in SIDECAR_EXTS:
                if not include_sidecars:
                    used.add(p)
                    continue
                meta = Metadata(camera="Unassociated Sidecar", capture_date=datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"), metadata_source="filesystem")
                packages.append(ClipPackage(str(uuid.uuid4()), p, [p], "Unassociated Sidecars", meta, "unassociated sidecar", "n/a")); used.add(p)
            else:
                meta = Metadata(camera="Unidentified File Type", capture_date=datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"), metadata_source="filesystem fallback")
                packages.append(ClipPackage(str(uuid.uuid4()), p, [p], "Unidentified Files", meta, "unrecognized extension preserved", "unidentified")); used.add(p)
        return packages

    def _destination_dir(self, media_type: str, meta: Metadata, dt: datetime, config: JobConfig) -> Path:
        parts = [media_type]
        if media_type in {"Photos", "Videos", "Audio"} and config.group_camera:
            parts.append(safe_name(meta.camera))
        if config.group_date:
            if config.date_structure == "YYYY/MM/DD": parts.extend([dt.strftime("%Y"), dt.strftime("%m"), dt.strftime("%d")])
            elif config.date_structure == "YYYY/MMDD": parts.extend([dt.strftime("%Y"), dt.strftime("%m%d")])
            else: parts.append(dt.strftime("%Y%m%d"))
        return Path(*parts)

    def _build_destination_index(self, dest_root: Path) -> dict[int, list[Path]]:
        """Index all existing destination files by size without hashing them yet."""
        index: dict[int, list[Path]] = {}
        if not dest_root.exists():
            return index
        report_dir_name = "MediaOrganizer_Reports"
        for root, dirs, names in os.walk(dest_root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != report_dir_name]
            if self.cancel_event.is_set():
                raise CancelledError()
            for name in names:
                if name.startswith(".") or PARTIAL_SUFFIX in name:
                    continue
                path = Path(root) / name
                try:
                    if path.is_file():
                        index.setdefault(path.stat().st_size, []).append(path)
                except OSError:
                    continue
        self.log(f"Destination index ready: {dest_root} · {sum(len(v) for v in index.values())} files")
        return index

    def _cached_destination_checksum(self, path: Path, algorithm: str, engine: BackupEngine) -> str:
        try:
            stat = path.stat()
            key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, algorithm.lower())
        except OSError:
            return ""
        cache = getattr(self, "destination_hash_cache", {})
        if key not in cache:
            cache[key] = engine.checksum(path, algorithm, report_progress=True)
            self.destination_hash_cache = cache
        return cache[key]

    def _index_destination_file(self, dest_root: Path, path: Path) -> None:
        """Add a newly verified copy to the current job's global index."""
        try:
            resolved_root = str(dest_root.resolve())
            resolved_path = path.resolve()
            size = resolved_path.stat().st_size
        except OSError:
            return
        root_index = getattr(self, "destination_indexes", {}).setdefault(resolved_root, {})
        candidates = root_index.setdefault(size, [])
        if path not in candidates:
            # Preserve the lexical path beneath dest_root for reports. Resolving
            # /var to /private/var here would make Path.relative_to(dest_root)
            # fail even though the file is inside the selected destination.
            candidates.append(path)

    def _find_global_duplicate(self, dest_root: Path, source: Path, algorithm: str,
                               engine: BackupEngine) -> tuple[Path | None, str]:
        """Find exact source content anywhere inside destination, not only expected path."""
        try:
            size = source.stat().st_size
        except OSError:
            return None, ""
        root_key = str(dest_root.resolve())
        candidates = getattr(self, "destination_indexes", {}).get(root_key, {}).get(size, [])
        if not candidates:
            return None, ""
        self.set_status("CHECKING DESTINATION DUPLICATES")
        source_hash = engine.checksum(source, algorithm, report_progress=True)
        for existing in candidates:
            if self.cancel_event.is_set():
                raise CancelledError()
            try:
                if existing.exists() and self._cached_destination_checksum(existing, algorithm, engine) == source_hash:
                    return existing, source_hash
            except (OSError, PermissionError):
                continue
        return None, source_hash

    def _resolve_collision(self, dest_root: Path, dest_dir: Path, filename: str, source: Path, algorithm: str,
                           engine: BackupEngine, capture_dt: datetime, duplicate_policy: str = "skip") -> tuple[Path, str, str]:
        """Return (destination, action, checksum) without ever overwriting data.

        Exact-content handling is controlled by duplicate_policy:
        skip   -> use the existing verified file and create no new copy;
        keep   -> create a checksum-suffixed copy beside the existing file;
        folder -> create a checksum-suffixed copy in a Duplicates review folder.

        Same-name files with different content always receive a deterministic
        capture-time + checksum suffix.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        candidate = dest_dir / filename

        # Global duplicate search: for the recommended skip policy, inspect every
        # same-size file already present anywhere under this destination root.
        # This prevents a second copy when the user changes organization rules.
        if duplicate_policy == "skip":
            global_match, global_hash = self._find_global_duplicate(dest_root, source, algorithm, engine)
            if global_match is not None:
                return global_match, "duplicate", global_hash

        if not candidate.exists():
            return candidate, "new", ""

        self.set_status("CHECKING DUPLICATES")
        source_hash = engine.checksum(source, algorithm, report_progress=True)
        exact_match = False
        try:
            exact_match = (candidate.stat().st_size == source.stat().st_size and
                           engine.checksum(candidate, algorithm, report_progress=True) == source_hash)
        except Exception:
            exact_match = False

        stem, suffix = Path(filename).stem, Path(filename).suffix
        if exact_match:
            if duplicate_policy == "skip":
                return candidate, "duplicate", source_hash
            duplicate_dir = dest_dir / "Duplicates" if duplicate_policy == "folder" else dest_dir
            duplicate_dir.mkdir(parents=True, exist_ok=True)
            base = duplicate_dir / f"{stem}__duplicate__{source_hash[:8]}{suffix}"
            if not base.exists():
                return base, "duplicate_folder" if duplicate_policy == "folder" else "duplicate_keep", source_hash
            try:
                if base.stat().st_size == source.stat().st_size and engine.checksum(base, algorithm, report_progress=True) == source_hash:
                    return base, "duplicate", source_hash
            except Exception:
                pass
            i = 2
            while True:
                alt = duplicate_dir / f"{stem}__duplicate__{source_hash[:8]}__{i}{suffix}"
                if not alt.exists():
                    return alt, "duplicate_folder" if duplicate_policy == "folder" else "duplicate_keep", source_hash
                try:
                    if alt.stat().st_size == source.stat().st_size and engine.checksum(alt, algorithm, report_progress=True) == source_hash:
                        return alt, "duplicate", source_hash
                except Exception:
                    pass
                i += 1

        stamp = capture_dt.strftime("%Y%m%d_%H%M%S") if capture_dt else "unknown_date"
        short_hash = source_hash[:8]
        renamed = dest_dir / f"{stem}__{stamp}__{short_hash}{suffix}"
        if not renamed.exists():
            return renamed, "renamed", source_hash
        try:
            if renamed.stat().st_size == source.stat().st_size and engine.checksum(renamed, algorithm, report_progress=True) == source_hash:
                if duplicate_policy == "skip":
                    return renamed, "duplicate", source_hash
        except Exception:
            pass
        i = 2
        while True:
            alt = dest_dir / f"{stem}__{stamp}__{short_hash}__{i}{suffix}"
            if not alt.exists():
                return alt, "renamed", source_hash
            try:
                if alt.stat().st_size == source.stat().st_size and engine.checksum(alt, algorithm, report_progress=True) == source_hash:
                    if duplicate_policy == "skip":
                        return alt, "duplicate", source_hash
            except Exception:
                pass
            i += 1

    def _advance_bytes(self, amount: int):
        self.done_bytes += amount
        now = time.monotonic()
        self.speed_samples.append((now, self.done_bytes))
        cutoff = now - 20.0
        self.speed_samples = [x for x in self.speed_samples if x[0] >= cutoff]
        self.ui_queue.put(("progress", self.done_bytes, max(self.total_work_bytes, 1), list(self.speed_samples)))

    def _validated_sources(self) -> tuple[Path, ...]:
        if not self.source_paths:
            raw = self.source_var.get().strip()
            candidate = Path(raw).expanduser() if raw else None
            if candidate and candidate.is_dir():
                self._set_source_paths([candidate])
        if not self.source_paths:
            raise ValueError("请选择至少一个有效的源素材文件夹。" if self.language_var.get() == "中文" else "Select at least one valid source media folder.")
        invalid = [p for p in self.source_paths if not p.is_dir()]
        if invalid:
            raise ValueError(f"Source folder is unavailable: {invalid[0]}")
        return tuple(p.resolve() for p in self.source_paths)

    def _snapshot_config(self) -> JobConfig:
        sources = self._validated_sources()
        destinations = [Path(x).expanduser().resolve() for x in (self.dest1_var.get(), self.dest2_var.get()) if x.strip()]
        if not destinations: raise ValueError("Select at least one destination.")
        if len(set(map(str, destinations))) != len(destinations): raise ValueError("Backup destinations must be different.")
        for dest in destinations:
            for source in sources:
                if paths_overlap(source, dest):
                    raise ValueError(f"Source and destination folders cannot overlap: {source} ↔ {dest}")
            dest.mkdir(parents=True, exist_ok=True)
            if not os.access(dest, os.W_OK): raise ValueError(f"Destination is not writable: {dest}")
        report_formats = tuple(fmt for fmt, enabled in (("csv", self.report_csv_var.get()), ("json", self.report_json_var.get()), ("html", self.report_html_var.get())) if enabled)
        if self.report_var.get() and not report_formats:
            raise ValueError("请至少选择一种报告格式。" if self.language_var.get() == "中文" else "Select at least one report format.")
        return JobConfig(tuple(map(str, sources)), tuple(map(str, destinations)), self.operation_var.get(), self.group_camera_var.get(),
                         self.group_date_var.get(), self.date_structure_var.get(), self.checksum_var.get(),
                         self.sidecars_var.get(), self.report_var.get(), self.duplicate_policy_var.get(), report_formats)

    @staticmethod
    def _media_type(path: Path) -> str:
        ext = path.suffix.lower()
        if ext in PHOTO_EXTS: return "Photos"
        if ext in VIDEO_EXTS: return "Videos"
        if ext in AUDIO_EXTS: return "Audio"
        return "Unidentified Files"

    def show_selected_metadata(self, _event=None):
        selected = self.tree.selection()
        if not selected: return
        path = Path(selected[0])
        try:
            meta = self.metadata_engine.read(path)
            pkg = self.package_by_primary.get(str(path))
            payload = {"File": str(path), "Size": format_bytes(path.stat().st_size), "Camera": meta.camera,
                       "Make": meta.make, "Model": meta.model, "Lens": meta.lens,
                       "Capture date": meta.capture_date, "Metadata source": meta.metadata_source}
            if pkg:
                payload["Clip package"] = {"association": pkg.association, "camera_confidence": pkg.camera_confidence,
                                           "members": [str(x) for x in pkg.members]}
            if meta.raw:
                payload["Selected raw fields"] = {k: meta.raw.get(k) for k in ("Make", "Model", "CameraModelName", "UniqueCameraModel", "DateTimeOriginal", "CreateDate", "LensModel") if meta.raw.get(k)}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            text = f"Unable to read metadata: {e}"
        self.inspector.configure(state="normal"); self.inspector.delete("1.0", "end"); self.inspector.insert("1.0", text); self.inspector.configure(state="disabled")

    @staticmethod
    def _format_duration(seconds: int) -> str:
        h, rem = divmod(max(0, seconds), 3600); m, s = divmod(rem, 60)
        if h: return f"{h}h {m:02d}m"
        if m: return f"{m}m {s:02d}s"
        return f"{s}s"

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set(); self.set_status("STOP REQUESTED")

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self.start_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _drain_ui_queue(self):
        try:
            while True:
                msg = self.ui_queue.get_nowait(); kind = msg[0]
                if kind == "log":
                    self.log_text.configure(state="normal"); self.log_text.insert("end", msg[1] + "\n"); self.log_text.see("end"); self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(msg[1]); self.status_label.configure(text=msg[1])
                elif kind == "package":
                    _, pkg, idx, total = msg
                    p, meta = pkg.primary, pkg.metadata
                    dt = meta.capture_date.replace("T", " ")[:16] if meta.capture_date else "—"
                    label = p.name if len(pkg.members) == 1 else f"{p.name}  (+{len(pkg.members)-1})"
                    self.tree.insert("", "end", iid=str(p), text=label, values=(pkg.media_type, meta.camera, dt, format_bytes(pkg.total_size), "Ready"))
                    self.progress_text_var.set(f"{idx}/{total} packages · {format_bytes(self.total_bytes)}")
                elif kind == "item_status":
                    iid, status = msg[1], msg[2]
                    if self.tree.exists(iid):
                        values = list(self.tree.item(iid, "values")); values[-1] = status; self.tree.item(iid, values=values)
                elif kind == "progress":
                    done, total, samples = msg[1], max(msg[2], 1), msg[3]
                    pct = min(100.0, done / total * 100)
                    self.progress["value"] = pct; self.progress_text_var.set(f"{pct:.1f}% · {format_bytes(done)} / {format_bytes(total)}")
                    speed = 0.0
                    if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
                        speed = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
                    if speed > 0:
                        seconds = max(0, int((total - done) / speed))
                        remaining = self._format_duration(seconds)
                        finish = (datetime.now().timestamp() + seconds)
                        finish_text = datetime.fromtimestamp(finish).strftime("%H:%M:%S")
                        self.eta_text_var.set(self.tr("eta").format(remaining=remaining, finish=finish_text, speed=format_bytes(int(speed))))
                elif kind == "running": self._set_running(msg[1])
                elif kind == "session_finished":
                    finished_session = msg[1]
                    if finished_session == self.session_id:
                        self.last_completed_session = finished_session
                        self._set_running(False)
                        # The worker is at the end of its finally block. Clear the reference shortly
                        # afterwards so a second ingest starts with a clean lifecycle.
                        self.after(50, lambda sid=finished_session: self._release_finished_worker(sid))
                elif kind == "complete":
                    failed, reports = msg[1], msg[2]
                    skipped = msg[3] if len(msg) > 3 else 0
                    if self.language_var.get() == "中文":
                        message = "备份与校验已完成。" if failed == 0 and skipped == 0 else (f"任务完成，跳过了 {skipped} 个暂时不可访问的源文件。" if failed == 0 else f"任务完成，但有 {failed} 个副本失败；对应源文件已保留。")
                    else:
                        message = "Ingest finished successfully." if failed == 0 and skipped == 0 else (f"Ingest finished; skipped {skipped} temporarily unavailable source file(s)." if failed == 0 else f"Ingest finished with {failed} failed copy/copies. Source files for failed items were retained.")
                    messagebox.showinfo(APP_NAME, message)
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def _release_finished_worker(self, session_id: int):
        if session_id != self.session_id:
            return
        if self.worker and self.worker.is_alive():
            self.after(50, lambda sid=session_id: self._release_finished_worker(sid))
            return
        self.worker = None

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A task is running. Request a safe stop and close after the worker exits?"):
                return
            self.cancel_event.set()
            self.after(200, self._wait_close)
        else:
            self.destroy()

    def _wait_close(self):
        if self.worker and self.worker.is_alive(): self.after(200, self._wait_close)
        else: self.destroy()


def main():
    app = MediaOrganizerApp(); app.mainloop()

if __name__ == "__main__":
    main()
