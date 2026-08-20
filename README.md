# flpackager

Package an FL Studio project so it opens cleanly on someone else's machine.

FL Studio stores sample references as **absolute paths on the creator's
machine**. Send a collaborator a bare `.flp` and they get a wall of
missing-audio errors. `flpackager` bundles the project with its samples,
rewrites the paths to point at the bundled copies, and writes a manifest of
everything the receiver still needs (plugins, factory content, anything that
couldn't be found).

---

## Install

Requires Python 3.8+.

```bash
pip install -r requirements.txt
```

Or install the CLI onto your PATH:

```bash
pip install -e .
```

Without installing, run it straight from the repo root with `python -m flpackager.cli`.

---

## The app (GUI)

For most people this is the whole product. Double-click `flpackager.exe` — no
Python, no terminal.

The interface is a dark, FL-inspired skin built on
[CustomTkinter](https://github.com/TomSchimansky/CustomTkinter): a thin title
bar, a left sidebar, and two screens.

**Library** — every project in a folder, newest first:

```
 C  Crate                                   17 to bundle (17.2 MB)   v0.1.0
+---------+--------------------------------------------------------------+
|>Library |  Library                                                     |
| Incoming|  * C:\Users\you\Music  [Change folder] [Open a package...]    |
| Settings|  573 projects - showing the 200 most recent                   |
|         |  PROJECT                    BPM     KEY    SAMPLES   STATUS   |
|         |  * night drive              142   G# minor      5  (2 missing)|
|         |    edited 20 hours ago                                       |
|         |  * clean one                 90   F major       1     Ready   |
|  Kamel  |  * feerer                   127   A minor       8     Ready   |
+---------+--------------------------------------------------------------+
```

Rows highlight on hover; clicking one opens the project. A project with
unresolved samples shows an amber **N missing** instead of *Ready*, so "can I
send this?" is answerable from the list without opening anything.

**Project page** — what's in it, and the one action that matters:

```
  <-  Library
  *  scaryahh                                    [ ^  Package & Send ]
  +-----++------------------++---------++---------+  Package will include:
  | BPM || KEY              || SAMPLES || PLUGINS |  17 samples - 17.2 MB
  | 139 || C minor estimated||   19    ||   11    |  Save to: ...\Desktop
  +-----++------------------++---------++---------+

  Samples | Plugins
  --------
   Kick - Alot.wav                          37.7 KB   ( Bundled )
   C:\Users\you\Music\...\Kicks\Kick - Alot.wav
   clap.wav                                      --   ( Built-in )
   vox chop 3.wav                                --   ( Missing )
```

Status pills read at a glance: green **Bundled**, blue **Built-in**, red
**Missing**. The **Plugins** tab opens with a banner explaining that plugins
can't be bundled and that the receiver needs them installed, then lists each
one as **Native** or **Third-party**. Anything the old *Notes* tab used to
explain — missing samples, renamed files, parser warnings — now appears as a
banner above the list it concerns.

**Package & Send** opens a progress modal driven by real per-file progress,
ending in a green check, *Package ready*, and a **Reveal in folder** button.

Orange is rationed deliberately: the logo, the Package & Send button, the
active tab underline, and progress bars. Everything else stays neutral grey, so
the one action that matters is the one thing that draws the eye.

**Run it from source:**

```bash
pip install -r requirements.txt
python -m flpackager
```

### Build the standalone .exe

```bash
pip install pyinstaller customtkinter
pyinstaller flpackager.spec --noconfirm
```

Produces `dist/flpackager.exe` — a single ~11 MB file with Python, PyFLP and
CustomTkinter baked in, and no console window. The same binary also serves the
CLI: `flpackager.exe pack project.flp`.

The spec file handles the one thing PyInstaller can't infer on its own:
CustomTkinter loads its theme JSON and fonts *from disk* at import time, so
those data files have to be collected explicitly. `flpackager.spec` does it
with:

```python
from PyInstaller.utils.hooks import collect_data_files
ctk_datas = collect_data_files("customtkinter")   # -> Analysis(datas=ctk_datas)
```

If you build without the spec, pass the same thing on the command line, or the
.exe will start and immediately die looking for its theme:

```bash
pyinstaller --onefile --windowed --collect-data customtkinter flpackager/__main__.py
```

Sanity-check a fresh build by double-clicking `dist\flpackager.exe`: if the
theme data didn't make it in, the process exits instead of drawing a window.

Drag-and-drop activates automatically when `tkinterdnd2` is installed
(`pip install tkinterdnd2`) — drop an `.flp` anywhere on the main window.
Without it, use the folder chip or **Change folder** in the Library.

All disk work runs on a worker thread and reports back through a queue, so the
window stays responsive: the folder listing appears immediately, each project's
BPM / key / sample count is read in the background and only for the rows
actually on screen, and packing and unpacking track real per-file progress.
Reading a `.flp` is CPU-bound and holds the GIL, which is why the Library reads
on demand rather than parsing a whole folder up front, and caps the list at the
200 most recently edited projects.

---

## Usage (command line)

### `pack` — bundle a project

```bash
flpackager pack "C:\beats\my track.flp" -o ./out
```

Produces:

```
out/
  my track_package/
      my track.flp        <- copy, with sample paths rewritten to ./samples/
      samples/            <- every referenced sample, copied in
      manifest.txt        <- BPM, key, plugins, missing samples
      manifest.json       <- the same data, machine-readable
  my track_package.zip    <- the whole bundle, ready to send
```

Options:

| Flag | Effect |
|---|---|
| `-o, --output DIR` | Where to write (default: current directory) |
| `--dry-run` | Report what *would* be bundled. Writes nothing at all. |
| `--no-zip` | Build the bundle folder but skip the `.zip` |
| `-q, --quiet` | Hide the progress line |

### `unpack` — open a package someone sent you

```bash
flpackager unpack "my track_package.zip" -o ./received
```

Extracts so the `.flp` and its `samples/` folder sit together, then prints the
manifest (BPM, estimated key, required plugins, missing samples). Open the
extracted `.flp` in FL Studio and it finds its audio.

---

## What ends up in the manifest

```
Project name   : scaryahh
Tempo (BPM)    : 139.0
Key            : C minor (estimated, confidence 0.87, from 16 notes)

Required plugins (11)
  NATIVE  Sytrus  (channel)
  VST     Serum  [Xfer Records]  (channel)
  ...

Bundled samples (17, 17.2 MB)
  samples/Kick - Alot.wav
      from: C:\Users\...\Kicks\Kick - Alot.wav
  ...

Built-in / factory samples (2) - no copy needed
  %FLStudioFactoryData%\...\Basic 808 Kick.wav

MISSING samples (0) - not found on the packaging machine
  (none - every referenced sample was found)
```

---

## Safety guarantee

**Your original `.flp` and your original samples are never modified.** They are
opened read-only; everything is written to new files under the output
directory. `pack` refuses to run if the output directory would contain the
source project. This is enforced and covered by tests
(`test_original_flp_is_never_modified`, `test_original_samples_are_never_modified`).

### Why not `pyflp.save()`?

`pyflp.save()` re-serialises every event from its parsed form, and that is not
lossless. Tested against 471 real projects:

| Event | Problem |
|---|---|
| `PlaylistEvent` (233) | **lost 16 bytes** — PyFLP's `GreedyRange` silently drops a trailing partial item, i.e. dropped playlist data |
| `TrackEvent` (238) | floats came back re-encoded (`0x3f814afd` → `0x3f81ae47`) |
| `ParametersEvent` (215) | differing tail bytes |

**52 of 471 projects did not round-trip byte-for-byte.**

So `flpackager` doesn't use it. Instead it copies the original event stream
verbatim and splices in replacements *only* for the specific `SamplePath`
events it intends to change ([`flpackager/flpwriter.py`](flpackager/flpwriter.py)).
Verified: reassembling all 471 projects with zero replacements reproduces every
file byte-for-byte, and a real pack changes **only** id-196 events.

If the writer is ever handed an index that isn't a `SamplePath` event it raises
rather than write a file it might be corrupting; `build_package` then falls back
to bundling the `.flp` unchanged and says so in the manifest.

---

## Edge cases handled

- **Missing samples** — listed in the manifest under *MISSING samples* with
  their original paths. Their channel keeps the original path so it can be
  relinked in FL. Packaging does not fail.
- **Factory / built-in samples** — `%FLStudioFactoryData%` paths (and absolute
  paths that land inside an FL install) are detected, left untouched, and listed
  as *no copy needed*. The receiver's own FL Studio resolves them.
- **`%FLStudioUserData%`** — your own rendered/recorded audio. This *is*
  resolved and bundled, since the receiver won't have it.
- **Duplicate filenames from different folders** — de-duplicated by resolved
  absolute path. Two different `kick.wav` files become `kick.wav` and
  `kick_2.wav`; neither overwrites the other, and each channel points at the
  right one.
- **The same file used by several channels** — copied once, shared.
- **Large sample sets** — copies stream in chunks, audio is *stored* rather
  than deflated in the zip (much faster, barely larger), and a progress line
  reports each file.
- **Unparseable regions** — parse failures are collected as warnings rather
  than raised, so a partially-readable project still packages. Warnings appear
  in the manifest.
- **Zip-slip** — `unpack` refuses absolute paths and `..` traversal in archives.

---

## Using the core from other code

The CLI is a thin layer over `flpackager.core`, which is UI-agnostic and
returns dataclasses rather than printing — a GUI can reuse it as-is.

```python
from flpackager import core

analysis = core.analyze_project("my track.flp")   # reads, writes nothing
analysis.tempo                 # 139.0
analysis.key                   # KeyEstimate(key='C minor', confidence=0.87, ...)
analysis.plugins               # [PluginRef(name='Sytrus', kind='native', ...), ...]
analysis.unique_bundled        # [SampleRef(...), ...]
analysis.missing               # samples not found on this machine
analysis.warnings              # anything that couldn't be read

result = core.build_package(analysis, "out", progress=lambda d, t, s: None)
result.zip_path

core.unpack_package(result.zip_path, "received")
```

Key functions: `analyze_project()`, `collect_samples()`, `collect_plugins()`,
`estimate_key()`, `build_package()`, `unpack_package()`, and the `pack()`
convenience wrapper.

---

## Key estimation

Best-effort and **always labelled "estimated"**. Duration-weighted pitch-class
histogram matched against the Krumhansl-Kessler profiles. Rack channels using
fewer than three distinct pitch classes are skipped as percussion, since drum
one-shots would otherwise dominate the histogram. With under 12 tonal notes it
reports no key rather than guessing.

Treat it as a hint, not a fact.

---

## Environment overrides

If FL Studio is installed somewhere unusual:

- `FLPACKAGER_FACTORY_DATA` — path `%FLStudioFactoryData%` resolves to
- `FLPACKAGER_USER_DATA` — path `%FLStudioUserData%` resolves to

---

## Tests

```bash
python -m unittest discover -s tests -v
```

17 tests covering de-duplication, missing/factory samples, dry-run, zip-slip,
the never-modify-originals guarantee, and byte-level writer fidelity.

The GUI has its own driver that clicks through the real widgets and worker
threads from code — load a project, package it, unpack it, and feed it bad
input:

```bash
python tools/gui_smoke.py
```

Development scripts in `tools/` were used to validate against a real corpus:

| Script | Purpose |
|---|---|
| `recon.py` | Dump what PyFLP exposes from a `.flp` |
| `sweep.py` | Failure-mode census across a folder of projects |
| `roundtrip.py` | Shows `pyflp.save()` is lossy |
| `diffevents.py` | Pinpoints which event IDs don't round-trip |
| `verify_identity.py` | Proves our writer is byte-exact |
| `stress.py` | Full pack+unpack validation over many real projects |
| `gui_smoke.py` | Drives the GUI end to end (load, package, unpack, errors) |
| `recover_probe.py` | Measures how many missing samples are findable on disk |

```bash
python tools/stress.py "C:\path\to\projects" 30
```

---

## Scope (v0)

Out of scope for now: repairing the user's originals, version diffing, cloud
sync, DAWs other than FL Studio.

The Library screen is a browser over one folder, not a managed library: it
lists what's on disk, reads each project on demand, and forgets it all when you
close the window. The only thing it remembers between runs is which folder you
pointed it at (in `~/.flpackager-gui.json`).

Deliberately **not** built, with reasons:

* **Bulk "find my missing samples"** — measured against a real 471-project
  library: of 2883 missing references, only 37 (1%) were findable anywhere on
  disk, and recovering them would have fixed *zero* additional projects. Those
  samples were deleted, not moved. A *per-project* relink offered during `pack`
  is the version worth building, since a recently-reorganised folder is a much
  better bet than a decade-old one.
* **Bundling plugins** — you can't legally redistribute someone's VST. The
  manifest names them instead; preset state already travels inside the `.flp`.
* **Batch project health check** — the Library surfaces a per-project *N
  missing* badge as you browse, which covers the "can I send this?" question.
  A bulk report across a whole folder is still library management, and already
  answerable with `pack --dry-run`.

### Version coverage

The test corpus is FL Studio 21.1 and 25.1 only, so older projects are
unproven. The byte-level writer is inherently robust here though: events it
doesn't recognise are copied verbatim rather than re-encoded, so unfamiliar
structures from an older FL pass through untouched. The only thing it must get
right is locating `SamplePath` events — a single stable event ID. The
pre-FL-11.5 ASCII string path exists but has never run against a real file.
