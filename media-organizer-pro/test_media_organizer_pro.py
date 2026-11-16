from __future__ import annotations

import csv
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path

import media_organizer_pro as mop


class FakeMetadataEngine:
    def __init__(self, metadata: mop.Metadata | None = None):
        self.metadata = metadata or mop.Metadata(
            camera="Test Camera",
            make="Test",
            model="Camera",
            capture_date="2026-08-06T12:45:10",
            metadata_source="test",
            raw={},
        )

    def read(self, _path: Path) -> mop.Metadata:
        return self.metadata


def make_app(test_root: Path, packages: list[mop.ClipPackage]) -> mop.MediaOrganizerApp:
    app = object.__new__(mop.MediaOrganizerApp)
    app.cancel_event = threading.Event()
    app.ui_queue = queue.Queue()
    app.records = []
    app.clip_packages = packages
    app.scanned_files = [package.primary for package in packages]
    app.package_by_primary = {str(package.primary): package for package in packages}
    app.metadata_engine = FakeMetadataEngine()
    app.video_resolver = mop.VideoMetadataResolver(app.metadata_engine)
    app.audio_resolver = mop.AudioMetadataResolver()
    app.db = mop.AssetDatabase(test_root / "library.sqlite3")
    app.total_bytes = sum(package.total_size for package in packages)
    app.done_bytes = 0
    app.total_work_bytes = app.total_bytes * 2
    app.transfer_started_monotonic = 0.0
    app.speed_samples = []
    app.session_id = 1
    return app


def make_package(path: Path, *, camera: str = "Test Camera") -> mop.ClipPackage:
    metadata = mop.Metadata(
        camera=camera,
        make="Test",
        model="Camera",
        capture_date="2026-08-06T12:45:10",
        metadata_source="test",
        raw={},
    )
    return mop.ClipPackage(str(path), path, [path], "Videos", metadata, "standalone", "confirmed")


def make_config(
    sources: tuple[Path, ...],
    destinations: tuple[Path, ...],
    *,
    group_camera: bool = False,
    group_date: bool = False,
    date_structure: str = "YYYY/MM/DD",
    generate_report: bool = False,
    include_sidecars: bool = True,
) -> mop.JobConfig:
    return mop.JobConfig(
        tuple(str(path) for path in sources),
        tuple(str(path) for path in destinations),
        "copy",
        group_camera,
        group_date,
        date_structure,
        "sha256",
        include_sidecars,
        generate_report,
        "skip",
        ("csv", "json", "html"),
    )


class MetadataIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_exiftool_runs_even_when_parent_locale_is_c_utf_8(self):
        exiftool = shutil.which("exiftool")
        if not exiftool:
            self.skipTest("ExifTool is not installed")
        media = self.root / "metadata.wav"
        with wave.open(str(media), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\0\0" * 480)

        db = mop.AssetDatabase(self.root / "metadata.sqlite3")
        engine = mop.MetadataEngine(db)
        old_values = {name: os.environ.get(name) for name in ("LC_ALL", "LC_CTYPE", "LANG")}
        try:
            os.environ["LC_ALL"] = "C.UTF-8"
            os.environ["LC_CTYPE"] = "C.UTF-8"
            os.environ["LANG"] = "C.UTF-8"
            result = engine._read_exiftool(media)
        finally:
            for name, value in old_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertIsNotNone(result)
        self.assertEqual("ExifTool", result.metadata_source)

    def test_audio_recognizes_tascam_x6_and_x8(self):
        resolver = mop.AudioMetadataResolver()
        x6, confidence6 = resolver.resolve(
            Path("FIELD_Portacapture_X6.wav"),
            mop.Metadata(raw={"Originator": "TASCAM Portacapture X6"}),
        )
        x8, confidence8 = resolver.resolve(
            Path("PC-X8_take01.wav"),
            mop.Metadata(raw={"Software": "TASCAM"}),
        )
        self.assertEqual("TASCAM Portacapture X6", x6.camera)
        self.assertEqual("TASCAM Portacapture X8", x8.camera)
        self.assertIn(confidence6, {"confirmed", "probable"})
        self.assertIn(confidence8, {"confirmed", "probable"})

    def test_unknown_device_fingerprints_split_devices_but_ignore_clip_fields(self):
        first = {"Manufacturer": "TASCAM", "Firmware": "1.0", "FileName": "A.wav", "CreateDate": "2026:01:01 10:00:00"}
        same_device = {"Manufacturer": "TASCAM", "Firmware": "1.0", "FileName": "B.wav", "CreateDate": "2026:01:02 11:00:00"}
        other_device = {"Manufacturer": "TASCAM", "Firmware": "2.0", "FileName": "C.wav"}
        self.assertEqual(
            mop.device_fingerprint(first, "audio", "TASCAM", ".wav"),
            mop.device_fingerprint(same_device, "audio", "TASCAM", ".wav"),
        )
        self.assertNotEqual(
            mop.device_fingerprint(first, "audio", "TASCAM", ".wav"),
            mop.device_fingerprint(other_device, "audio", "TASCAM", ".wav"),
        )

    def test_source_and_destination_overlap_is_rejected_in_both_directions(self):
        source = self.root / "Photos"
        child = source / "Organized"
        parent = self.root
        sibling = self.root / "Backup"
        self.assertTrue(mop.paths_overlap(source, source))
        self.assertTrue(mop.paths_overlap(source, child))
        self.assertTrue(mop.paths_overlap(source, parent))
        self.assertFalse(mop.paths_overlap(source, sibling))


class PackageBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_video_audio_and_xml_with_same_clip_key_form_one_package(self):
        video = self.root / "A001_C001.mp4"
        audio = self.root / "A001_C001_audio.wav"
        xml = self.root / "A001_C001.xml"
        unrelated = self.root / "other.wav"
        for path in (video, audio, xml, unrelated):
            path.write_bytes(path.name.encode())
        app = make_app(self.root, [])
        packages = app._build_clip_packages([video, audio, xml, unrelated])
        video_package = next(package for package in packages if package.primary == video)
        self.assertEqual({video, audio, xml}, set(video_package.members))
        self.assertTrue(any(package.primary == unrelated for package in packages))

    def test_disabling_sidecars_excludes_xml_but_keeps_companion_audio(self):
        video = self.root / "B001_C001.mp4"
        audio = self.root / "B001_C001_audio.wav"
        xml = self.root / "B001_C001.xml"
        for path in (video, audio, xml):
            path.write_bytes(path.name.encode())
        app = make_app(self.root, [])
        packages = app._build_clip_packages([video, audio, xml], include_sidecars=False)
        video_package = next(package for package in packages if package.primary == video)
        self.assertEqual({video, audio}, set(video_package.members))
        self.assertFalse(any(xml in package.members for package in packages))

    def test_missing_files_are_ignored_during_package_build(self):
        missing = self.root / "disconnected-volume" / "missing.CR3"
        app = make_app(self.root, [])
        app.metadata_engine = mop.MetadataEngine(mop.AssetDatabase(self.root / "missing.sqlite3"))
        app.video_resolver = mop.VideoMetadataResolver(app.metadata_engine)
        packages = app._build_clip_packages([missing])
        self.assertEqual([], packages)


class TransferIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _source(self, folder: str, name: str, payload: bytes) -> Path:
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    @staticmethod
    def _media_files(destination: Path) -> list[Path]:
        return [path for path in destination.rglob("*") if path.is_file() and "MediaOrganizer_Reports" not in path.parts]

    def test_same_job_multi_source_exact_content_is_copied_only_once(self):
        first = self._source("card-a", "A001.mp4", b"same-media-content")
        second = self._source("card-b", "renamed-clip.mov", b"same-media-content")
        destination = self.root / "backup"
        packages = [make_package(first), make_package(second)]
        app = make_app(self.root, packages)
        app.total_work_bytes = app.total_bytes * 2
        app._transfer_worker(make_config((first.parent, second.parent), (destination,)), own_running_state=False)
        self.assertEqual(1, len(self._media_files(destination)))
        self.assertEqual(["DUPLICATE_SKIPPED", "VERIFIED"], sorted(record.status for record in app.records))
        duplicate = next(record for record in app.records if record.status == "DUPLICATE_SKIPPED")
        self.assertTrue(Path(duplicate.destination).exists())

    def test_second_import_after_layout_change_finds_duplicate_anywhere(self):
        source = self._source("card", "clip.mp4", b"second-import-content")
        destination = self.root / "backup"
        package = make_package(source, camera="Camera One")

        first_app = make_app(self.root / "first", [package])
        first_app._transfer_worker(
            make_config((source.parent,), (destination,), group_camera=True, group_date=True),
            own_running_state=False,
        )
        second_app = make_app(self.root / "second", [package])
        second_app._transfer_worker(
            make_config((source.parent,), (destination,), group_camera=False, group_date=False),
            own_running_state=False,
        )
        self.assertEqual(1, len(self._media_files(destination)))
        self.assertEqual("DUPLICATE_SKIPPED", second_app.records[0].status)
        self.assertIn("Camera One", second_app.records[0].destination)

    def test_two_destinations_each_receive_a_verified_copy(self):
        source = self._source("card", "clip.mp4", b"two-destination-content")
        first_destination = self.root / "backup-a"
        second_destination = self.root / "backup-b"
        package = make_package(source)
        app = make_app(self.root, [package])
        app.total_work_bytes = app.total_bytes * 4
        app._transfer_worker(
            make_config((source.parent,), (first_destination, second_destination)),
            own_running_state=False,
        )
        self.assertEqual(1, len(self._media_files(first_destination)))
        self.assertEqual(1, len(self._media_files(second_destination)))
        self.assertEqual(["VERIFIED", "VERIFIED"], [record.status for record in app.records])

    def test_csv_json_html_and_duplicate_reports_are_generated(self):
        source = self._source("card", "clip.mp4", b"report-content")
        destination = self.root / "backup"
        package = make_package(source)

        first_app = make_app(self.root / "first", [package])
        first_app._transfer_worker(
            make_config((source.parent,), (destination,), generate_report=True),
            own_running_state=False,
        )
        second_app = make_app(self.root / "second", [package])
        second_app._transfer_worker(
            make_config((source.parent,), (destination,), generate_report=True),
            own_running_state=False,
        )

        report_root = destination / "MediaOrganizer_Reports"
        for extension in ("csv", "json", "html"):
            self.assertTrue(list(report_root.glob(f"MediaOrganizer_Verification_*.{extension}")))
            self.assertTrue(list((report_root / "Duplicates").glob(f"Duplicate_Files_*.{extension}")))
        duplicate_json = sorted((report_root / "Duplicates").glob("Duplicate_Files_*.json"))[-1]
        payload = json.loads(duplicate_json.read_text(encoding="utf-8"))
        self.assertEqual("duplicates", payload["report_type"])
        self.assertEqual("DUPLICATE_SKIPPED", payload["records"][0]["status"])
        with sorted((report_root / "Duplicates").glob("Duplicate_Files_*.csv"))[-1].open(encoding="utf-8-sig") as handle:
            self.assertEqual("DUPLICATE_SKIPPED", next(csv.DictReader(handle))["status"])

    def test_progress_budget_and_eta_helpers_cover_copy_and_verification(self):
        self.assertEqual("59s", mop.MediaOrganizerApp._format_duration(59))
        self.assertEqual("1m 01s", mop.MediaOrganizerApp._format_duration(61))
        self.assertEqual("1h 01m", mop.MediaOrganizerApp._format_duration(3661))
        source = self._source("card", "large.mp4", b"x" * (5 * 1024 * 1024))
        destination = self.root / "backup"
        package = make_package(source)
        app = make_app(self.root, [package])
        app._transfer_worker(make_config((source.parent,), (destination,)), own_running_state=False)
        self.assertEqual(app.total_work_bytes, app.done_bytes)
        self.assertGreaterEqual(len(app.speed_samples), 2)

    def test_missing_source_after_scan_is_skipped_without_failing_the_job(self):
        missing = self.root / "disconnected-volume" / "missing.mp4"
        destination = self.root / "backup"
        app = make_app(self.root, [make_package(missing)])
        app._transfer_worker(make_config((missing.parent,), (destination,)), own_running_state=False)
        queued = []
        while True:
            try:
                queued.append(app.ui_queue.get_nowait())
            except queue.Empty:
                break
        messages = "\n".join(str(item) for item in queued)
        self.assertEqual([], app.records)
        self.assertIn("source unavailable", messages.lower())
        self.assertNotIn("('status', 'ERROR')", messages)

    def test_missing_package_member_is_reported_without_aborting_other_members(self):
        primary = self._source("card", "clip.mp4", b"primary-content")
        missing_member = self.root / "card" / "clip.xml"
        destination = self.root / "backup"
        package = make_package(primary)
        package.members = [primary, missing_member]
        app = make_app(self.root, [package])
        app._transfer_worker(make_config((primary.parent,), (destination,)), own_running_state=False)
        self.assertEqual(["VERIFIED", "SKIPPED_MISSING"], [record.status for record in app.records])


if __name__ == "__main__":
    unittest.main(verbosity=2)
