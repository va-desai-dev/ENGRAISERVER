"""The autobiographical ledger: dated facts about the person ENGRAI serves.

Written by hand in the console, indexed by a private embedding worker, and
added to chat requests on the routes that opt in, so the model knows its user
whichever front end is talking to the gateway.
"""

from .router import create_ledger
from .service import Ledger, LedgerError

__all__ = ["Ledger", "LedgerError", "create_ledger"]
