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

__all__ = ['Engine', 'AxeEngine', 'IbmEngine', 'HtmlcsEngine', 'AlfaEngine']
