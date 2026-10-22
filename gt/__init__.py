"""game-translator core package.

Layout contract: nothing in `gt/` may mention a specific game, game path or
DOM selector. All game data lives in `profiles/<id>/`; all host-specific code
lives in `adapters/` and `backends/`.
"""

__version__ = "0.2.3"
