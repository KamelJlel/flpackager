"""Core, UI-agnostic packaging logic for FL Studio projects.

Everything here returns structured data (dataclasses) rather than printing, so
the CLI -- and a future GUI -- can render it however they like.

Safety guarantee
----------------
Nothing in this module ever writes to, moves, or modifies the user's original
.flp or their original sample files. Originals are opened read-only; all output
goes to newly created files under the chosen output directory.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import warnings
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .flpwriter import FLPStructureError, rewrite_sample_paths

Path = pathlib.Path

#: Called as ``progress(done, total, label)``. Purely for UI feedback.
ProgressFn = Callable[[int, int, str], None]

SAMPLES_DIRNAME = "samples"
MANIFEST_TXT = "manifest.txt"
MANIFEST_JSON = "manifest.json"

# --- sample status values ---------------------------------------------------
FOUND = "found"        # exists on disk, will be bundled
MISSING = "missing"    # referenced but not on this machine
BUILTIN = "builtin"    # ships with FL Studio; receiver already has it

DUPLICATE_NOTE = "duplicate reference; bundled once"

# --- FL Studio path variables ----------------------------------------------
FACTORY_VAR = "%FLStudioFactoryData%"
USER_VAR = "%FLStudioUserData%"

AUDIO_SUFFIXES = frozenset(
    {
        ".wav", ".mp3", ".ogg", ".flac", ".aiff", ".aif",
        ".aac", ".m4a", ".wv", ".rex", ".rx2", ".ds",
    }
)


# ===========================================================================
# Data model
# ===========================================================================


@dataclass
class SampleRef:
    """One sample file referenced by the project."""

    original_path: str
    """Exactly as stored in the .flp (may contain %FLStudioFactoryData% etc.)."""

    status: str
    """One of :data:`FOUND`, :data:`MISSING`, :data:`BUILTIN`."""

    channel_name: str = ""
    channel_iid: Optional[int] = None

    resolved_path: Optional[str] = None
    """Absolute path after expanding FL path variables, if we could resolve it."""

    bundled_name: Optional[str] = None
    """Filename inside ``samples/``. De-duplicated; None unless bundled."""

    size_bytes: Optional[int] = None

    event_index: Optional[int] = None
    """Index of this path's event in the .flp event stream (for rewriting)."""

    note: str = ""


@dataclass
class PluginRef:
    """A plugin/instrument the project needs in order to open correctly."""

    name: str
    kind: str  # "vst" | "native"
    vendor: Optional[str] = None
    used_in: List[str] = field(default_factory=list)  # "channel" / "mixer"


@dataclass
class KeyEstimate:
    """A best-effort musical key. Always label this as estimated to the user."""

    key: str                 # e.g. "F# minor"
    confidence: float        # 0..1, correlation-derived
    note_count: int
    estimated: bool = True


@dataclass
class ProjectAnalysis:
    """Everything we learned about a project, without having written anything."""

    flp_path: str
    project_name: str
    tempo: Optional[float] = None
    key: Optional[KeyEstimate] = None
    fl_version: Optional[str] = None
    unicode_strings: bool = True
    samples: List[SampleRef] = field(default_factory=list)
    plugins: List[PluginRef] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # -- convenience views, handy for both CLI and GUI --
    @property
    def bundled(self) -> List[SampleRef]:
        return [s for s in self.samples if s.status == FOUND]

    @property
    def missing(self) -> List[SampleRef]:
        return [s for s in self.samples if s.status == MISSING]

    @property
    def builtin(self) -> List[SampleRef]:
        return [s for s in self.samples if s.status == BUILTIN]

    @property
    def unique_bundled(self) -> List[SampleRef]:
        """Bundled samples minus repeat references to the same file."""
        return [s for s in self.bundled if s.note != DUPLICATE_NOTE]

    @property
    def total_bundle_bytes(self) -> int:
        return sum(s.size_bytes or 0 for s in self.unique_bundled)


@dataclass
class PackageResult:
    analysis: ProjectAnalysis
    bundle_dir: Optional[str]
    zip_path: Optional[str]
    copied_files: int
    copied_bytes: int
    manifest_text: str
    dry_run: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class UnpackResult:
    output_dir: str
    flp_path: Optional[str]
    sample_count: int
    manifest_text: str
    manifest: Optional[Dict[str, Any]] = None
    warnings: List[str] = field(default_factory=list)


# ===========================================================================
# FL Studio path variable resolution
# ===========================================================================


def _candidate_factory_dirs() -> List[Path]:
    """Locations that ``%FLStudioFactoryData%`` might point at."""
    override = os.environ.get("FLPACKAGER_FACTORY_DATA")
    if override:
        return [Path(override)]

    found: List[Path] = []
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "/Applications",
    ]
    for root in roots:
        if not root:
            continue
        base = Path(root) / "Image-Line"
        try:
            if base.is_dir():
                # Newest install first ("FL Studio 2025" sorts above "FL Studio 21").
                found.extend(sorted((p for p in base.iterdir() if p.is_dir()), reverse=True))
        except OSError:
            continue
    return found


def _candidate_user_dirs() -> List[Path]:
    """Locations that ``%FLStudioUserData%`` might point at."""
    override = os.environ.get("FLPACKAGER_USER_DATA")
    if override:
        return [Path(override)]

    home = Path.home()
    return [
        home / "Documents" / "Image-Line" / "FL Studio",
        home / "Documents" / "Image-Line",
    ]


def _expand_fl_variables(raw: str) -> Tuple[Optional[Path], bool]:
    """Expand FL's ``%...%`` path variables.

    Returns ``(resolved_path_or_None, is_factory)``.
    """
    text = raw.strip()
    lowered = text.lower()

    if lowered.startswith(FACTORY_VAR.lower()):
        tail = text[len(FACTORY_VAR):].lstrip("\\/")
        for base in _candidate_factory_dirs():
            candidate = base / tail.replace("\\", os.sep)
            if candidate.exists():
                return candidate, True
        return None, True  # still known-factory even if we can't locate FL

    if lowered.startswith(USER_VAR.lower()):
        # The user's own rendered/recorded audio -- this DOES need bundling.
        tail = text[len(USER_VAR):].lstrip("\\/")
        for base in _candidate_user_dirs():
            candidate = base / tail.replace("\\", os.sep)
            if candidate.exists():
                return candidate, False
        return None, False

    # A plain path. On Windows these are already absolute; normalise separators
    # so a project made on the other OS still resolves where possible.
    native = text.replace("\\", os.sep) if os.sep == "/" else text
    return Path(native), False


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child_r = os.path.normcase(os.path.abspath(str(child)))
        parent_r = os.path.normcase(os.path.abspath(str(parent)))
    except (OSError, ValueError):
        return False
    return child_r == parent_r or child_r.startswith(parent_r.rstrip(os.sep) + os.sep)


def _looks_like_factory_path(resolved: Optional[Path]) -> bool:
    """Catch factory content referenced by absolute path instead of the variable."""
    if resolved is None:
        return False
    return any(_is_inside(resolved, base) for base in _candidate_factory_dirs())


# ===========================================================================
# Key estimation
# ===========================================================================

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Kessler key profiles.
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 5.19, 2.39, 3.66, 2.29, 2.88, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

MIN_NOTES_FOR_KEY = 12


def _correlate(histogram: Sequence[float], profile: Sequence[float]) -> float:
    n = len(histogram)
    mean_h = sum(histogram) / n
    mean_p = sum(profile) / n
    num = sum((histogram[i] - mean_h) * (profile[i] - mean_p) for i in range(n))
    den_h = sum((histogram[i] - mean_h) ** 2 for i in range(n)) ** 0.5
    den_p = sum((profile[i] - mean_p) ** 2 for i in range(n)) ** 0.5
    if den_h == 0 or den_p == 0:
        return 0.0
    return num / (den_h * den_p)


def estimate_key(project: Any, warns: Optional[List[str]] = None) -> Optional[KeyEstimate]:
    """Estimate the musical key from pattern MIDI notes. Best-effort.

    Percussion is filtered out heuristically: a rack channel that only ever
    plays one or two distinct pitch classes is almost certainly a drum one-shot
    rather than tonal material, and would only pollute the histogram.

    Returns None when there isn't enough tonal content to make a claim.
    """
    warns = warns if warns is not None else []
    per_channel: Dict[int, Dict[int, float]] = {}

    try:
        patterns = list(project.patterns)
    except Exception as exc:
        warns.append(f"Couldn't read patterns for key estimation: {exc}")
        return None

    for pattern in patterns:
        try:
            notes = list(pattern.notes)
        except Exception as exc:
            warns.append(f"Couldn't read notes in a pattern for key estimation: {exc}")
            continue
        for note in notes:
            try:
                pitch = int(note["key"]) % 12
                channel = int(note.rack_channel)
                weight = float(note.length or 0) or 1.0
            except Exception:
                continue
            per_channel.setdefault(channel, {})
            per_channel[channel][pitch] = per_channel[channel].get(pitch, 0.0) + weight

    histogram = [0.0] * 12
    counted = 0
    for pitches in per_channel.values():
        if len(pitches) < 3:
            continue  # percussion / single-pitch channel
        for pitch, weight in pitches.items():
            histogram[pitch] += weight
            counted += 1

    if sum(histogram) <= 0 or counted < MIN_NOTES_FOR_KEY:
        return None

    best: Optional[Tuple[float, str]] = None
    scores: List[float] = []
    for tonic in range(12):
        rotated = histogram[tonic:] + histogram[:tonic]
        for profile, quality in ((_MAJOR_PROFILE, "major"), (_MINOR_PROFILE, "minor")):
            score = _correlate(rotated, profile)
            scores.append(score)
            if best is None or score > best[0]:
                best = (score, f"{_NOTE_NAMES[tonic]} {quality}")

    if best is None or best[0] <= 0:
        return None

    # Confidence blends absolute fit with how far it beats the runner-up.
    scores.sort(reverse=True)
    margin = scores[0] - scores[1] if len(scores) > 1 else 0.0
    confidence = max(0.0, min(1.0, best[0] * 0.8 + margin * 2.0))
    return KeyEstimate(key=best[1], confidence=round(confidence, 3), note_count=counted)


# ===========================================================================
# Analysis
# ===========================================================================


def _event_index_of_sample_path(channel: Any) -> Optional[int]:
    """Root-stream index of this channel's SamplePath event.

    PyFLP's ``EventTree`` records each event's root index in ``IndexedEvent.r``,
    which is exactly the index our byte-level writer addresses.
    """
    from pyflp.channel import ChannelID

    try:
        for indexed in channel.events.lst:
            if indexed.e.id == ChannelID.SamplePath:
                return int(indexed.r)
    except Exception:
        return None
    return None


def _dedupe_name(raw_name: str, taken: Dict[str, int]) -> str:
    """Give each distinct source file a unique name inside ``samples/``."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name).strip() or "sample"
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""

    key = name.lower()
    if key not in taken:
        taken[key] = 1
        return name

    while True:
        taken[key] += 1
        candidate = f"{stem}_{taken[key]}" + (f".{suffix}" if suffix else "")
        if candidate.lower() not in taken:
            taken[candidate.lower()] = 1
            return candidate


def collect_samples(project: Any, warns: Optional[List[str]] = None) -> List[SampleRef]:
    """Find every sample the project references, classify and de-duplicate it.

    De-duplication is by resolved absolute path, so two channels pointing at the
    same file share one bundled copy, while two *different* files that happen to
    share a filename get distinct names (``kick.wav`` / ``kick_2.wav``) instead
    of one silently overwriting the other.
    """
    from pyflp.channel import Sampler

    warns = warns if warns is not None else []
    refs: List[SampleRef] = []
    by_resolved: Dict[str, SampleRef] = {}
    taken_names: Dict[str, int] = {}

    try:
        channels = list(project.channels)
    except Exception as exc:
        warns.append(f"Couldn't read the channel rack: {exc}")
        return refs

    for channel in channels:
        if not isinstance(channel, Sampler):
            continue

        try:
            label = channel.display_name or ""
        except Exception:
            label = ""
        try:
            iid = channel.iid
        except Exception:
            iid = None

        try:
            sample_path = channel.sample_path
        except Exception as exc:
            warns.append(f"Couldn't read the sample path for channel {label or iid!r}: {exc}")
            continue

        if sample_path is None:
            continue
        raw = str(sample_path)
        if raw in ("", "."):
            continue  # empty Sampler slot

        resolved, is_factory = _expand_fl_variables(raw)
        if not is_factory and _looks_like_factory_path(resolved):
            is_factory = True

        ref = SampleRef(
            original_path=raw,
            status=BUILTIN if is_factory else MISSING,
            channel_name=label,
            channel_iid=iid,
            resolved_path=str(resolved) if resolved else None,
            event_index=_event_index_of_sample_path(channel),
        )

        if is_factory:
            ref.note = "ships with FL Studio"
            refs.append(ref)
            continue

        exists = False
        if resolved is not None:
            try:
                exists = resolved.is_file()
            except OSError:
                exists = False

        if not exists:
            ref.status = MISSING
            refs.append(ref)
            continue

        key = os.path.normcase(os.path.abspath(str(resolved)))
        twin = by_resolved.get(key)
        if twin is not None:
            # Same file referenced again: reuse the single bundled copy.
            ref.status = FOUND
            ref.bundled_name = twin.bundled_name
            ref.size_bytes = twin.size_bytes
            ref.note = DUPLICATE_NOTE
            refs.append(ref)
            continue

        ref.status = FOUND
        ref.bundled_name = _dedupe_name(resolved.name, taken_names)
        try:
            ref.size_bytes = resolved.stat().st_size
        except OSError:
            ref.size_bytes = None
        if ref.bundled_name.lower() != resolved.name.lower():
            ref.note = f"renamed from {resolved.name} to avoid a filename clash"
        by_resolved[key] = ref
        refs.append(ref)

    return refs


def collect_plugins(project: Any, warns: Optional[List[str]] = None) -> List[PluginRef]:
    """Collect the instruments and effects the project needs.

    Covers both channel-rack instruments and mixer insert effect slots, since a
    project is equally broken if either is missing on the other machine.
    """
    from pyflp.channel import Instrument
    from pyflp.plugin import VSTPlugin

    warns = warns if warns is not None else []
    found: Dict[Tuple[str, str], PluginRef] = {}

    def record(name: str, kind: str, vendor: Optional[str], where: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        key = (kind, name.lower())
        ref = found.get(key)
        if ref is None:
            found[key] = PluginRef(name=name, kind=kind, vendor=vendor, used_in=[where])
            return
        if vendor and not ref.vendor:
            ref.vendor = vendor
        if where not in ref.used_in:
            ref.used_in.append(where)

    # --- channel rack instruments ---
    try:
        channels = list(project.channels)
    except Exception as exc:
        warns.append(f"Couldn't read the channel rack for plugins: {exc}")
        channels = []

    for channel in channels:
        try:
            internal = channel.internal_name or ""
        except Exception:
            internal = ""

        if isinstance(channel, Instrument):
            plugin = None
            try:
                plugin = channel.plugin
            except Exception as exc:
                warns.append(f"Couldn't read the plugin on channel {internal or '?'}: {exc}")
            if isinstance(plugin, VSTPlugin):
                try:
                    record(plugin.name or internal, "vst", plugin.vendor, "channel")
                    continue
                except Exception:
                    pass
            if internal and internal != "Fruity Wrapper":
                record(internal, "native", None, "channel")
            elif internal == "Fruity Wrapper":
                try:
                    fallback = channel.display_name or ""
                except Exception:
                    fallback = ""
                record(fallback or "Unknown VST", "vst", None, "channel")
        elif internal:
            record(internal, "native", None, "channel")

    # --- mixer effect slots ---
    try:
        inserts = list(project.mixer)
    except Exception as exc:
        warns.append(f"Couldn't read the mixer: {exc}")
        inserts = []

    for insert in inserts:
        try:
            slots = list(insert)
        except Exception:
            # Some projects lack the mixer params event; PyFLP raises KeyError.
            continue
        for slot in slots:
            try:
                internal = slot.internal_name or ""
            except Exception:
                continue
            if not internal:
                continue

            if internal != "Fruity Wrapper":
                record(internal, "native", None, "mixer")
                continue

            plugin = None
            try:
                plugin = slot.plugin
            except Exception:
                pass
            if isinstance(plugin, VSTPlugin):
                try:
                    record(plugin.name or "Unknown VST", "vst", plugin.vendor, "mixer")
                    continue
                except Exception:
                    pass
            try:
                fallback = slot.name or ""
            except Exception:
                fallback = ""
            record(fallback or "Unknown VST", "vst", None, "mixer")

    return sorted(found.values(), key=lambda p: (p.kind, p.name.lower()))


def analyze_project(flp_path: "os.PathLike[str] | str") -> ProjectAnalysis:
    """Read a project and report what packaging it would involve.

    Opens the .flp read-only and writes nothing. Individual failures are
    collected into ``ProjectAnalysis.warnings`` rather than raised, so a
    partially-readable project still produces a usable result.
    """
    import pyflp

    source = Path(flp_path)
    warns: List[str] = []

    if not source.is_file():
        raise FileNotFoundError(f"No such .flp file: {source}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        project = pyflp.parse(str(source))
        for entry in caught:
            warns.append(f"PyFLP: {entry.message}")

    analysis = ProjectAnalysis(flp_path=str(source.resolve()), project_name=source.stem)

    try:
        title = (project.title or "").strip()
        if title:
            analysis.project_name = title
    except Exception as exc:
        warns.append(f"Couldn't read the project title: {exc}")

    try:
        analysis.tempo = float(project.tempo) if project.tempo is not None else None
    except Exception as exc:
        warns.append(f"Couldn't read the tempo: {exc}")

    try:
        version = project.version
        analysis.fl_version = str(version)
        analysis.unicode_strings = (version.major, version.minor) >= (11, 5)
    except Exception as exc:
        warns.append(f"Couldn't read the FL Studio version: {exc}")

    analysis.samples = collect_samples(project, warns)
    analysis.plugins = collect_plugins(project, warns)

    try:
        analysis.key = estimate_key(project, warns)
    except Exception as exc:
        warns.append(f"Key estimation failed: {exc}")

    analysis.warnings = warns
    return analysis


# ===========================================================================
# Manifest rendering
# ===========================================================================


def _human_bytes(size: Optional[int]) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def render_manifest(analysis: ProjectAnalysis, *, flp_name: str) -> str:
    """Render the human-readable manifest.txt content."""
    lines: List[str] = []
    add = lines.append

    add("FL Studio project package")
    add("=" * 40)
    add("")
    add(f"Project name   : {analysis.project_name}")
    add(f"Project file   : {flp_name}")
    add(f"Source .flp    : {analysis.flp_path}")
    if analysis.fl_version:
        add(f"Made with FL   : {analysis.fl_version}")
    add(f"Tempo (BPM)    : {analysis.tempo if analysis.tempo is not None else 'unknown'}")
    if analysis.key:
        add(
            f"Key            : {analysis.key.key} (estimated, "
            f"confidence {analysis.key.confidence:.2f}, "
            f"from {analysis.key.note_count} notes)"
        )
    else:
        add("Key            : not estimated (no clear tonal content)")
    add(f"Packaged       : {datetime.datetime.now().isoformat(timespec='seconds')}")
    add(f"Packaged by    : flpackager {__version__}")
    add("")

    add(f"Required plugins ({len(analysis.plugins)})")
    add("-" * 40)
    if analysis.plugins:
        for plugin in analysis.plugins:
            vendor = f"  [{plugin.vendor}]" if plugin.vendor else ""
            add(f"  {plugin.kind.upper():7} {plugin.name}{vendor}  ({', '.join(plugin.used_in)})")
    else:
        add("  (none detected)")
    add("")

    unique = analysis.unique_bundled
    add(f"Bundled samples ({len(unique)}, {_human_bytes(analysis.total_bundle_bytes)})")
    add("-" * 40)
    if unique:
        for ref in unique:
            add(f"  {SAMPLES_DIRNAME}/{ref.bundled_name}")
            add(f"      from: {ref.original_path}")
            if ref.note:
                add(f"      note: {ref.note}")
    else:
        add("  (none)")
    add("")

    builtin = analysis.builtin
    add(f"Built-in / factory samples ({len(builtin)}) - no copy needed")
    add("-" * 40)
    if builtin:
        for ref in builtin:
            add(f"  {ref.original_path}")
    else:
        add("  (none)")
    add("")

    missing = analysis.missing
    add(f"MISSING samples ({len(missing)}) - not found on the packaging machine")
    add("-" * 40)
    if missing:
        add("  These could not be located and are NOT in this package.")
        add("  The paths below are where they lived on the original machine.")
        add("")
        for ref in missing:
            who = f'  (channel "{ref.channel_name}")' if ref.channel_name else ""
            add(f"  {ref.original_path}{who}")
            if ref.note:
                add(f"      note: {ref.note}")
    else:
        add("  (none - every referenced sample was found)")
    add("")

    if analysis.warnings:
        add(f"Warnings ({len(analysis.warnings)})")
        add("-" * 40)
        for warning in analysis.warnings:
            add(f"  {warning}")
        add("")

    return "\n".join(lines) + "\n"


def _manifest_data(analysis: ProjectAnalysis, flp_name: str) -> Dict[str, Any]:
    data = asdict(analysis)
    data["flp_name"] = flp_name
    data["tool_version"] = __version__
    data["packaged_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return data


# ===========================================================================
# Packaging
# ===========================================================================


def _safe_relpath(name: str) -> bool:
    """Reject absolute paths and ``..`` traversal (zip-slip protection)."""
    if not name or name.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    return ".." not in re.split(r"[\\/]+", name)


def build_package(
    analysis: ProjectAnalysis,
    output_dir: "os.PathLike[str] | str",
    *,
    dry_run: bool = False,
    progress: Optional[ProgressFn] = None,
    make_zip: bool = True,
) -> PackageResult:
    """Build the bundle (and zip) described by ``analysis``.

    Writes only inside ``output_dir``. The original .flp and the original
    samples are read but never modified.
    """
    source = Path(analysis.flp_path)
    out_root = Path(output_dir)
    flp_name = f"{source.stem}.flp"
    bundle_name = f"{source.stem}_package"
    bundle_dir = out_root / bundle_name
    zip_path = out_root / f"{bundle_name}.zip"
    warns: List[str] = []

    to_copy = [
        ref for ref in analysis.unique_bundled if ref.resolved_path
    ]

    if dry_run:
        return PackageResult(
            analysis=analysis,
            bundle_dir=None,
            zip_path=None,
            copied_files=0,
            copied_bytes=0,
            manifest_text=render_manifest(analysis, flp_name=flp_name),
            dry_run=True,
            warnings=warns,
        )

    # --- safety: never write on top of the source project ---
    if _is_inside(source, bundle_dir):
        raise ValueError(
            "Refusing to run: the output bundle directory would contain the "
            "original .flp. Choose a different output directory."
        )

    samples_dir = bundle_dir / SAMPLES_DIRNAME
    samples_dir.mkdir(parents=True, exist_ok=True)

    # --- copy samples ---
    copied_files = 0
    copied_bytes = 0
    total = len(to_copy)
    for index, ref in enumerate(to_copy, start=1):
        src = Path(ref.resolved_path)  # type: ignore[arg-type]
        dst = samples_dir / (ref.bundled_name or src.name)
        if progress:
            progress(index, total, f"copying {src.name}")
        try:
            # copyfile streams in chunks; fine for multi-GB sample sets.
            shutil.copyfile(str(src), str(dst))
            shutil.copystat(str(src), str(dst))
            copied_files += 1
            copied_bytes += ref.size_bytes or 0
        except OSError as exc:
            ref.status = MISSING
            ref.note = f"copy failed: {exc}"
            warns.append(f"Couldn't copy {src}: {exc}")

    # --- write the rewritten .flp copy ---
    # Missing and factory samples deliberately keep their original paths: the
    # receiver's FL Studio resolves factory content itself, and a missing file
    # is easier to relink when the original path is still visible.
    replacements: Dict[int, str] = {}
    for ref in analysis.samples:
        if ref.status != FOUND or ref.event_index is None or not ref.bundled_name:
            continue
        replacements[ref.event_index] = f"{SAMPLES_DIRNAME}\\{ref.bundled_name}"

    original_bytes = source.read_bytes()
    try:
        new_bytes = rewrite_sample_paths(
            original_bytes, replacements, unicode=analysis.unicode_strings
        )
    except FLPStructureError as exc:
        warns.append(
            f"Couldn't rewrite sample paths ({exc}); bundling the .flp unchanged. "
            "Samples are in ./samples/ but you may need to relink them in FL Studio."
        )
        new_bytes = original_bytes

    (bundle_dir / flp_name).write_bytes(new_bytes)

    # Rendered last so any copy failures above are reflected in the manifest.
    manifest_text = render_manifest(analysis, flp_name=flp_name)
    (bundle_dir / MANIFEST_TXT).write_text(manifest_text, encoding="utf-8")
    (bundle_dir / MANIFEST_JSON).write_text(
        json.dumps(_manifest_data(analysis, flp_name), indent=2, default=str),
        encoding="utf-8",
    )

    # --- zip it ---
    final_zip: Optional[Path] = None
    if make_zip:
        entries: List[Tuple[Path, str]] = []
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                entries.append((path, str(path.relative_to(bundle_dir)).replace(os.sep, "/")))

        with zipfile.ZipFile(str(zip_path), "w", allowZip64=True) as archive:
            for index, (path, arcname) in enumerate(entries, start=1):
                if progress:
                    progress(index, len(entries), f"zipping {arcname}")
                # Audio barely compresses; storing it is much faster.
                method = (
                    zipfile.ZIP_STORED
                    if path.suffix.lower() in AUDIO_SUFFIXES
                    else zipfile.ZIP_DEFLATED
                )
                archive.write(str(path), arcname, compress_type=method)
        final_zip = zip_path

    return PackageResult(
        analysis=analysis,
        bundle_dir=str(bundle_dir),
        zip_path=str(final_zip) if final_zip else None,
        copied_files=copied_files,
        copied_bytes=copied_bytes,
        manifest_text=manifest_text,
        dry_run=False,
        warnings=warns,
    )


def pack(
    flp_path: "os.PathLike[str] | str",
    output_dir: "os.PathLike[str] | str",
    *,
    dry_run: bool = False,
    progress: Optional[ProgressFn] = None,
    make_zip: bool = True,
) -> PackageResult:
    """Convenience wrapper: :func:`analyze_project` then :func:`build_package`."""
    analysis = analyze_project(flp_path)
    return build_package(
        analysis, output_dir, dry_run=dry_run, progress=progress, make_zip=make_zip
    )


def unpack_package(
    zip_path: "os.PathLike[str] | str",
    output_dir: "os.PathLike[str] | str",
    *,
    progress: Optional[ProgressFn] = None,
) -> UnpackResult:
    """Extract a package so the .flp and its ``samples/`` folder sit together."""
    archive_path = Path(zip_path)
    if not archive_path.is_file():
        raise FileNotFoundError(f"No such package: {archive_path}")

    destination = Path(output_dir) / archive_path.stem
    destination.mkdir(parents=True, exist_ok=True)
    warns: List[str] = []
    sample_count = 0

    with zipfile.ZipFile(str(archive_path)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        for index, member in enumerate(members, start=1):
            if not _safe_relpath(member.filename):
                warns.append(f"Skipped unsafe path in archive: {member.filename}")
                continue
            if progress:
                progress(index, len(members), f"extracting {member.filename}")
            archive.extract(member, str(destination))
            if member.filename.startswith(SAMPLES_DIRNAME + "/"):
                sample_count += 1

    flp_files = sorted(destination.glob("*.flp"))
    if not flp_files:
        warns.append("No .flp file found in this package.")

    manifest_text = ""
    manifest_file = destination / MANIFEST_TXT
    if manifest_file.is_file():
        manifest_text = manifest_file.read_text(encoding="utf-8", errors="replace")
    else:
        warns.append("No manifest.txt found in this package.")

    manifest_data: Optional[Dict[str, Any]] = None
    manifest_json = destination / MANIFEST_JSON
    if manifest_json.is_file():
        try:
            manifest_data = json.loads(manifest_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warns.append(f"Couldn't read manifest.json: {exc}")

    return UnpackResult(
        output_dir=str(destination),
        flp_path=str(flp_files[0]) if flp_files else None,
        sample_count=sample_count,
        manifest_text=manifest_text,
        manifest=manifest_data,
        warnings=warns,
    )
