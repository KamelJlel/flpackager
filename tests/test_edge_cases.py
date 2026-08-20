"""Edge-case tests, built by synthesising .flp variants from a real project.

The interesting collision cases don't occur naturally in the sample corpus, so
we manufacture them: take a real project and rewrite its sample paths (using
our own byte-level writer) to point at files we control.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile

warnings.simplefilter("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyflp
from pyflp.channel import Sampler

from flpackager import core
from flpackager.flpwriter import (
    FLPStructureError,
    iter_raw_events,
    rewrite_sample_paths,
)

#: A real project with plenty of sampler channels.
SOURCE_FLP = os.environ.get(
    "FLPACKAGER_TEST_FLP",
    r"C:\Users\TRIEDENT\Desktop\Main\beats\scaryahh.flp",
)

WAV_HEADER = (
    b"RIFF" + struct.pack("<I", 36 + 8) + b"WAVEfmt "
    + struct.pack("<IHHIIHH", 16, 1, 1, 44100, 88200, 2, 16)
    + b"data" + struct.pack("<I", 8)
)


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fp:
        return fp.read()


def make_wav(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fp:
        fp.write(WAV_HEADER + payload)


def sample_path_indexes(data: bytes):
    return [i for i, e in enumerate(iter_raw_events(data)) if e.id == 196]


@unittest.skipUnless(os.path.isfile(SOURCE_FLP), f"needs a real .flp at {SOURCE_FLP}")
class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flpack_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.original = read_bytes(SOURCE_FLP)

    def _write_variant(self, replacements, name="variant.flp"):
        """Produce a .flp whose sample paths we control."""
        data = rewrite_sample_paths(self.original, replacements, unicode=True)
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fp:
            fp.write(data)
        return path

    def _paths_in(self, flp):
        out = []
        for channel in pyflp.parse(flp).channels:
            if isinstance(channel, Sampler):
                p = channel.sample_path
                if p is not None and str(p) not in ("", "."):
                    out.append(str(p))
        return out

    # -- duplicate filenames from different folders -------------------------

    def test_duplicate_filenames_are_deduplicated(self):
        a = os.path.join(self.tmp, "kits", "A", "kick.wav")
        b = os.path.join(self.tmp, "kits", "B", "kick.wav")
        make_wav(a, b"AAAAAAAA")
        make_wav(b, b"BBBBBBBB")

        indexes = sample_path_indexes(self.original)
        self.assertGreaterEqual(len(indexes), 2)
        flp = self._write_variant({indexes[0]: a, indexes[1]: b})

        analysis = core.analyze_project(flp)
        bundled = {r.resolved_path.lower(): r.bundled_name
                   for r in analysis.unique_bundled if r.resolved_path}
        self.assertIn(a.lower(), bundled)
        self.assertIn(b.lower(), bundled)

        name_a, name_b = bundled[a.lower()], bundled[b.lower()]
        self.assertNotEqual(name_a, name_b, "distinct files must not share a bundled name")

        out = os.path.join(self.tmp, "out")
        result = core.build_package(analysis, out, make_zip=False)
        samples = os.path.join(result.bundle_dir, "samples")

        # Both survived, with their own contents intact.
        self.assertEqual(read_bytes(os.path.join(samples, name_a))[-8:], b"AAAAAAAA")
        self.assertEqual(read_bytes(os.path.join(samples, name_b))[-8:], b"BBBBBBBB")

    def test_same_file_referenced_twice_is_bundled_once(self):
        shared = os.path.join(self.tmp, "shared", "loop.wav")
        make_wav(shared, b"SHARED12")

        indexes = sample_path_indexes(self.original)
        flp = self._write_variant({indexes[0]: shared, indexes[1]: shared})

        analysis = core.analyze_project(flp)
        refs = [r for r in analysis.samples
                if r.resolved_path and r.resolved_path.lower() == shared.lower()]
        self.assertEqual(len(refs), 2, "both channels should reference it")
        names = {r.bundled_name for r in refs}
        self.assertEqual(len(names), 1, "one physical copy shared by both channels")
        self.assertEqual(
            sum(1 for r in analysis.unique_bundled
                if r.resolved_path and r.resolved_path.lower() == shared.lower()),
            1,
            "it must only be copied once",
        )

    # -- missing samples ----------------------------------------------------

    def test_missing_samples_are_reported_not_fatal(self):
        gone = os.path.join(self.tmp, "nope", "vanished.wav")
        indexes = sample_path_indexes(self.original)
        flp = self._write_variant({indexes[0]: gone})

        analysis = core.analyze_project(flp)
        missing = [r for r in analysis.missing if r.original_path == gone]
        self.assertEqual(len(missing), 1)

        out = os.path.join(self.tmp, "out")
        result = core.build_package(analysis, out, make_zip=False)
        self.assertIn("MISSING samples", result.manifest_text)
        self.assertIn(gone, result.manifest_text)

        # The missing one keeps its original path so it can be relinked.
        packaged = os.path.join(result.bundle_dir, os.path.basename(flp))
        self.assertIn(gone, self._paths_in(packaged))

    # -- built-in / factory samples ----------------------------------------

    def test_factory_samples_are_not_bundled(self):
        factory = r"%FLStudioFactoryData%\Data\Patches\Packs\Legacy\Drums\Dance\Basic 808 Kick.wav"
        indexes = sample_path_indexes(self.original)
        flp = self._write_variant({indexes[0]: factory})

        analysis = core.analyze_project(flp)
        builtin = [r for r in analysis.builtin if r.original_path == factory]
        self.assertEqual(len(builtin), 1)
        self.assertNotIn(factory, [r.original_path for r in analysis.unique_bundled])

        out = os.path.join(self.tmp, "out")
        result = core.build_package(analysis, out, make_zip=False)
        # Untouched, so the receiver's own FL Studio resolves it.
        packaged = os.path.join(result.bundle_dir, os.path.basename(flp))
        self.assertIn(factory, self._paths_in(packaged))
        self.assertIn("no copy needed", result.manifest_text)

    # -- safety -------------------------------------------------------------

    def test_original_flp_is_never_modified(self):
        flp = self._write_variant({})
        before = read_bytes(flp)
        core.build_package(core.analyze_project(flp), os.path.join(self.tmp, "out"))
        self.assertEqual(read_bytes(flp), before)

    def test_original_samples_are_never_modified(self):
        src = os.path.join(self.tmp, "kit", "snare.wav")
        make_wav(src, b"ORIGINAL")
        indexes = sample_path_indexes(self.original)
        flp = self._write_variant({indexes[0]: src})

        before = read_bytes(src)
        core.build_package(core.analyze_project(flp), os.path.join(self.tmp, "out"))
        self.assertEqual(read_bytes(src), before)

    def test_dry_run_writes_nothing(self):
        flp = self._write_variant({})
        out = os.path.join(self.tmp, "dryrun_out")
        result = core.build_package(core.analyze_project(flp), out, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.bundle_dir)
        self.assertIsNone(result.zip_path)
        self.assertFalse(os.path.exists(out), "dry run must not create the output directory")
        self.assertTrue(result.manifest_text, "dry run still reports what it would do")

    def test_rewrite_is_byte_identical_with_no_replacements(self):
        self.assertEqual(rewrite_sample_paths(self.original, {}, unicode=True), self.original)

    def test_rewrite_refuses_a_wrong_event_index(self):
        # Index 0 is the FLP version event, not a SamplePath -- must refuse.
        with self.assertRaises(FLPStructureError):
            rewrite_sample_paths(self.original, {0: "samples/x.wav"}, unicode=True)

    def test_rewrite_touches_only_sample_path_events(self):
        indexes = sample_path_indexes(self.original)
        new = rewrite_sample_paths(self.original, {indexes[0]: "samples/x.wav"}, unicode=True)
        old_events = list(iter_raw_events(self.original))
        new_events = list(iter_raw_events(new))
        self.assertEqual(len(old_events), len(new_events))
        changed = {
            o.id for o, n in zip(old_events, new_events)
            if self.original[o.start:o.end] != new[n.start:n.end]
        }
        self.assertEqual(changed, {196})

    # -- unpack -------------------------------------------------------------

    def test_pack_unpack_round_trip(self):
        src = os.path.join(self.tmp, "kit", "clap.wav")
        make_wav(src, b"CLAPCLAP")
        indexes = sample_path_indexes(self.original)
        flp = self._write_variant({indexes[0]: src})

        out = os.path.join(self.tmp, "out")
        result = core.build_package(core.analyze_project(flp), out)
        self.assertTrue(os.path.isfile(result.zip_path))

        recv = os.path.join(self.tmp, "recv")
        unpacked = core.unpack_package(result.zip_path, recv)
        self.assertIsNotNone(unpacked.flp_path)
        self.assertIn("Tempo", unpacked.manifest_text)
        self.assertIsNotNone(unpacked.manifest)

        # Every rewritten path resolves next to the extracted .flp.
        base = os.path.dirname(unpacked.flp_path)
        for path in self._paths_in(unpacked.flp_path):
            if path.startswith("samples\\") or path.startswith("samples/"):
                target = os.path.join(base, path.replace("\\", os.sep))
                self.assertTrue(os.path.isfile(target), f"unresolved: {path}")

    def test_unpack_rejects_zip_slip(self):
        evil = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("../escaped.txt", "nope")
            archive.writestr("manifest.txt", "ok")
        recv = os.path.join(self.tmp, "recv")
        result = core.unpack_package(evil, recv)
        self.assertTrue(any("unsafe" in w for w in result.warnings))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "recv", "escaped.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))

    # -- misc ---------------------------------------------------------------

    def test_corrupt_file_raises_a_clear_error(self):
        bad = os.path.join(self.tmp, "bad.flp")
        with open(bad, "wb") as fp:
            fp.write(b"NOTANFLP" * 10)
        with self.assertRaises(Exception):
            core.analyze_project(bad)

    def test_analyze_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            core.analyze_project(os.path.join(self.tmp, "does_not_exist.flp"))

    def test_dedupe_name_generates_distinct_names(self):
        taken = {}
        names = [core._dedupe_name("kick.wav", taken) for _ in range(4)]
        self.assertEqual(len(set(names)), 4, names)
        self.assertEqual(names[0], "kick.wav")

    def test_dedupe_name_sanitises_illegal_characters(self):
        name = core._dedupe_name('we:ird*na|me.wav', {})
        for char in ':*|':
            self.assertNotIn(char, name)

    def test_key_estimate_is_labelled_estimated(self):
        analysis = core.analyze_project(SOURCE_FLP)
        if analysis.key is not None:
            self.assertTrue(analysis.key.estimated)
            self.assertGreaterEqual(analysis.key.confidence, 0.0)
            self.assertLessEqual(analysis.key.confidence, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
