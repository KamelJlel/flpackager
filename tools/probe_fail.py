import glob, os, sys, traceback, warnings
warnings.simplefilter("ignore")
import pyflp
from pyflp.channel import Instrument
for f in sorted(glob.glob(os.path.join(sys.argv[1], "*.flp"))):
    try: p = pyflp.parse(f)
    except Exception: continue
    try:
        for ins in p.mixer:
            for slot in ins: _ = slot.internal_name
    except Exception:
        print("MIXER", os.path.basename(f)); traceback.print_exc(limit=6); break
for f in sorted(glob.glob(os.path.join(sys.argv[1], "*.flp"))):
    try: p = pyflp.parse(f)
    except Exception: continue
    try: chans = list(p.channels)
    except Exception: continue
    for ch in chans:
        if isinstance(ch, Instrument):
            try: _ = ch.plugin
            except Exception:
                print("PLUGIN", os.path.basename(f)); traceback.print_exc(limit=6)
                sys.exit()
