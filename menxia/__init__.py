"""menxia -- adversarial review for AI coding agents.

Your agent writes its own summary of what it did. Of course it looks fine.
menxia hands the **raw session transcript** to models from other vendors and lets them veto.

Named after the Chancellery (門下省) of Tang China, whose defining power was not to advise
but to **refuse**: it could reject an imperial edict and send it back to be rewritten.
"""
__version__ = "0.1.0"

from menxia.council import READ_ONLY_TOOLS, STANCES, Seat, Verdict, assign_seats
from menxia.gate import Ledger, change_signature, install_hook, is_major
from menxia.slicer import slice_transcript, write_slice

__all__ = ["READ_ONLY_TOOLS", "STANCES", "Seat", "Verdict", "assign_seats",
           "Ledger", "change_signature", "install_hook", "is_major",
           "slice_transcript", "write_slice", "__version__"]
