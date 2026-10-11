"""Card Studio: character-card authoring on the gateway's own engines.

Transplanted from CardGen V2 (MIT, see web/LICENSE). The browser app lives in
``web/`` and is served as-is; this package replaces its Node server.
"""

from .router import create_cardgen

__all__ = ["create_cardgen"]
