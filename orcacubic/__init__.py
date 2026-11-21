"""OrcaCubic — Native Anycubic Cloud plugin for OrcaSlicer 2.5+ (macOS / Windows / Linux)."""

__version__ = "0.3.7"

# When imported as a wheel entry package inside OrcaSlicer 2.5+, register the plugin automatically.
try:
    import orca as _orca  # type: ignore[import-not-found]
except ImportError:
    _orca = None

if _orca is not None:
    try:
        from . import plugin as _orcacubic_plugin  # noqa: F401
    except ImportError:
        pass
