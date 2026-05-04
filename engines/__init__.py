"""
a11y-catscan engine package.

Four pluggable accessibility scan engines sharing one Chromium instance:

    AxeEngine    — axe-core (Deque), browser injection, 104 rules
    IbmEngine    — IBM Equal Access, browser injection, 158 rules
    HtmlcsEngine — HTML_CodeSniffer, browser injection, WCAG-only
    AlfaEngine   — Siteimprove Alfa, Node.js subprocess via CDP, ACT rules

All engines return normalized result dicts with EARL outcomes
(failed/cantTell/passed/inapplicable).  See engines/base.py for
the full result format specification.
"""

from .base import Engine
from .axe import AxeEngine
from .ibm import IbmEngine
from .htmlcs import HtmlcsEngine
from .alfa import AlfaEngine

# Registry of engine name → class.  Adding a new engine means adding
# one entry here plus the import above; Scanner.start() reads this
# table to instantiate engines by name.
ENGINES = {
    'axe': AxeEngine,
    'ibm': IbmEngine,
    'htmlcs': HtmlcsEngine,
    'alfa': AlfaEngine,
}

# Names of engines that accept the matching kwargs.  Engines with no
# entry here just receive the common ones (scan_level/verbose/quiet).
_EXTRA_KWARGS = {
    'axe': ('tags', 'rules'),
    'ibm': ('include_best',),
}


def make_engine(name, scan_level, *, verbose=False, quiet=False, **extras):
    """Instantiate an engine by name.

    Common kwargs (scan_level, verbose, quiet) go to every engine.
    Engine-specific kwargs (tags, rules, include_best) are forwarded
    only to engines that accept them; unknown extras are ignored so
    callers can pass a single bag of options.
    """
    cls = ENGINES.get(name)
    if cls is None:
        raise ValueError("Unknown engine: {}".format(name))
    accepted = _EXTRA_KWARGS.get(name, ())
    kwargs = {k: extras[k] for k in accepted if k in extras}
    return cls(scan_level, verbose=verbose, quiet=quiet, **kwargs)


__all__ = [
    'Engine', 'AxeEngine', 'IbmEngine', 'HtmlcsEngine', 'AlfaEngine',
    'ENGINES', 'make_engine',
]
