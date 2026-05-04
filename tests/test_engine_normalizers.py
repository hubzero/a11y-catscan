"""Tests for the per-engine result normalizers in `engines/*.py`.

The engines' `scan(page)` methods inject JS into a Playwright page
and process whatever the engine returns.  These tests use a mock
Page that no-ops the JS injection and returns hand-built engine
output, so the post-evaluate normalization logic (tag mapping, ARIA
classification, best-practice handling, EARL outcome translation)
gets exercised hermetically — no browser, no node_modules.

Each engine has slightly different result shapes:
  - axe:    {violations, incomplete, passes, inapplicable}
  - ibm:    {results: [{value, ruleId, path, message}]}
  - htmlcs: list of {type, code, msg, selector, html}
"""

from __future__ import annotations

import pytest

from engine_mappings import EARL_FAILED, EARL_CANTTELL


class _MockPage:
    """Minimal async Page stand-in.

    `add_script_tag` is a no-op; `evaluate` returns whatever was
    set via `set_evaluate_result` (or raises if `set_raise` was
    called).  Records the JS source and arg passed to evaluate
    so tests can assert the engine called the right thing.
    """

    def __init__(self):
        self._eval_result = None
        self._eval_raises = None
        self.evaluate_calls = []

    def set_evaluate_result(self, value):
        self._eval_result = value

    def set_evaluate_raises(self, exc):
        self._eval_raises = exc

    async def add_script_tag(self, *, content):
        # Engines call this to inject the bundled JS — no-op here.
        return None

    async def evaluate(self, source, *args):
        self.evaluate_calls.append((source, args))
        if self._eval_raises is not None:
            raise self._eval_raises
        return self._eval_result


# ── axe ─────────────────────────────────────────────────────────

class TestAxeNormalizer:
    """Cover the result-loop branches in `engines.axe.AxeEngine.scan`."""

    @pytest.fixture
    def engine(self):
        from engines.axe import AxeEngine
        eng = AxeEngine('wcag21aa', quiet=True)
        eng._source = '/* fake axe.min.js */'
        return eng

    async def test_normalizes_wcag_tags_and_outcomes(self, engine):
        page = _MockPage()
        page.set_evaluate_result({
            'violations': [{
                'id': 'image-alt',
                'tags': ['wcag2a', 'wcag111', 'cat.text-alternatives'],
                'impact': 'critical',
                'description': 'image needs alt',
                'help': 'Images must have alternative text',
                'helpUrl': 'https://example.test/image-alt',
                'nodes': [{'target': ['#hero'],
                           'html': '<img src="x">'}],
            }],
            'incomplete': [{
                'id': 'color-contrast',
                'tags': ['wcag2aa', 'wcag143'],
                'impact': 'serious',
                'nodes': [{'target': ['.btn']}],
            }],
            'passes': [],
            'inapplicable': [],
        })
        out = await engine.scan(page)

        # One violation + one incomplete
        failed = [i for i in out if i['outcome'] == EARL_FAILED]
        cant = [i for i in out if i['outcome'] == EARL_CANTTELL]
        assert len(failed) == 1
        assert len(cant) == 1

        v = failed[0]
        # Legacy wcag111 should normalize to sc-1.1.1; the
        # version/level tags wcag2a are dropped.
        assert 'sc-1.1.1' in v['tags']
        assert 'wcag2a' not in v['tags']
        assert v['engine'] == 'axe'

    async def test_best_practice_rule_gets_bp_tag(self, engine):
        # axe's `region` rule is best-practice / landmarks.
        page = _MockPage()
        page.set_evaluate_result({
            'violations': [{
                'id': 'region',
                'tags': ['cat.keyboard', 'best-practice'],
                'impact': 'moderate',
                'nodes': [{'target': ['main']}],
            }],
            'incomplete': [], 'passes': [], 'inapplicable': [],
        })
        out = await engine.scan(page)
        v = out[0]
        # bp_category('axe', 'region') == 'landmarks'
        assert 'bp-landmarks' in v['tags']
        # 'best-practice' is preserved (not duplicated)
        assert v['tags'].count('best-practice') == 1

    async def test_aria_rule_gets_aria_tag(self, engine):
        # 'aria-valid-attr' is in the ARIA validity bucket.
        page = _MockPage()
        page.set_evaluate_result({
            'violations': [{
                'id': 'aria-valid-attr',
                'tags': ['cat.aria', 'wcag412'],
                'impact': 'critical',
                'nodes': [{'target': ['nav']}],
            }],
            'incomplete': [], 'passes': [], 'inapplicable': [],
        })
        out = await engine.scan(page)
        v = out[0]
        # Should pick up an aria-* tag from aria_category('axe', ...)
        assert any(t.startswith('aria-') for t in v['tags'])

    async def test_axe_error_returns_empty(self, engine):
        # When axe.run() rejects, the JS catch returns
        # {error: '...'} — the scan should return [] and not
        # raise.
        page = _MockPage()
        page.set_evaluate_result({'error': 'simulated axe failure'})
        out = await engine.scan(page)
        assert out == []

    async def test_no_source_returns_empty(self, engine):
        # If start() never ran, scan should bail without
        # touching the page.
        engine._source = None
        page = _MockPage()
        out = await engine.scan(page)
        assert out == []
        assert page.evaluate_calls == []


# ── IBM Equal Access ────────────────────────────────────────────

class TestIbmNormalizer:
    """Cover the result-loop branches in `engines.ibm.IbmEngine.scan`."""

    @pytest.fixture
    def engine(self):
        from engines.ibm import IbmEngine
        eng = IbmEngine('wcag21aa', quiet=True)
        eng._source = '/* fake ace.js */'
        return eng

    async def test_violation_fail_becomes_failed(self, engine):
        page = _MockPage()
        page.set_evaluate_result({'results': [{
            'ruleId': 'text_contrast_sufficient',
            'value': ['VIOLATION', 'FAIL'],
            'path': {'dom': '/html/body/p[1]'},
            'message': 'contrast too low',
        }]})
        out = await engine.scan(page)
        assert len(out) == 1
        v = out[0]
        assert v['outcome'] == EARL_FAILED
        assert v['engine'] == 'ibm'
        # IBM's text_contrast_sufficient maps to SC 1.4.3
        assert 'sc-1.4.3' in v['tags']

    async def test_potential_becomes_canttell(self, engine):
        page = _MockPage()
        page.set_evaluate_result({'results': [{
            'ruleId': 'text_contrast_sufficient',
            'value': ['VIOLATION', 'POTENTIAL'],
            'path': {'dom': '/html/body/div'},
            'message': 'cannot determine contrast',
        }]})
        out = await engine.scan(page)
        assert len(out) == 1
        assert out[0]['outcome'] == EARL_CANTTELL

    async def test_recommendation_gates_on_include_best(self):
        # Non-VIOLATION (recommendation) should be skipped when
        # include_best is False, kept when include_best is True.
        from engines.ibm import IbmEngine
        results = {'results': [{
            'ruleId': 'aria_content_in_landmark',  # IBM bp rule
            'value': ['RECOMMENDATION', 'FAIL'],
            'path': {'dom': '/html/body'},
            'message': 'should be in landmark',
        }]}

        eng_no_bp = IbmEngine('wcag21aa', quiet=True,
                              include_best=False)
        eng_no_bp._source = '/* fake */'
        page = _MockPage()
        page.set_evaluate_result(results)
        out = await eng_no_bp.scan(page)
        assert out == []

        eng_with_bp = IbmEngine('wcag21aa', quiet=True,
                                include_best=True)
        eng_with_bp._source = '/* fake */'
        page2 = _MockPage()
        page2.set_evaluate_result(results)
        out = await eng_with_bp.scan(page2)
        assert len(out) == 1
        assert 'best-practice' in out[0]['tags']
        assert any(t.startswith('bp-') for t in out[0]['tags'])

    async def test_evaluate_exception_returns_empty(self, engine):
        # If the page.evaluate raises, the engine should swallow
        # it and return an empty list.  Test the verbose-print
        # branch by enabling verbose.
        engine.verbose = True
        engine.quiet = False
        page = _MockPage()
        page.set_evaluate_raises(RuntimeError('eval blew up'))
        out = await engine.scan(page)
        assert out == []


# ── HTML_CodeSniffer ────────────────────────────────────────────

class TestHtmlcsNormalizer:
    """Cover the result-loop branches in
    `engines.htmlcs.HtmlcsEngine.scan`."""

    @pytest.fixture
    def engine(self):
        from engines.htmlcs import HtmlcsEngine
        eng = HtmlcsEngine('wcag21aa', quiet=True)
        eng._source = '/* fake HTMLCS.js */'
        return eng

    async def test_error_type_becomes_failed(self, engine):
        page = _MockPage()
        page.set_evaluate_result([{
            'type': 1,  # ERROR
            'code': 'WCAG2AA.Principle1.Guideline1_1.1_1_1.H37',
            'msg': 'img needs alt',
            'selector': '#hero',
            'html': '<img>',
        }])
        out = await engine.scan(page)
        assert len(out) == 1
        v = out[0]
        assert v['outcome'] == EARL_FAILED
        assert v['engine'] == 'htmlcs'
        # 'H37' is the technique id at the end of the code chain
        assert v['id'] == 'H37'

    async def test_warning_type_becomes_canttell(self, engine):
        page = _MockPage()
        page.set_evaluate_result([{
            'type': 2,  # WARNING
            'code': 'WCAG2AA.Principle1.Guideline1_4.1_4_3.G18',
            'msg': 'verify contrast',
            'selector': '.btn',
            'html': '<button></button>',
        }])
        out = await engine.scan(page)
        assert len(out) == 1
        assert out[0]['outcome'] == EARL_CANTTELL

    async def test_other_type_is_dropped(self, engine):
        # type 3 = NOTICE; not surfaced.
        page = _MockPage()
        page.set_evaluate_result([{
            'type': 3, 'code': 'X', 'msg': 'fyi',
            'selector': '', 'html': '',
        }])
        out = await engine.scan(page)
        assert out == []

    async def test_no_selector_falls_back_to_code_in_target(
            self, engine):
        # When the message has no selector (no DOM element), the
        # node target falls back to the rule code.
        page = _MockPage()
        page.set_evaluate_result([{
            'type': 1,
            'code': 'WCAG2AA.Principle3.Guideline3_1.3_1_1.H57.2',
            'msg': 'html lang attr missing',
            'selector': '',
            'html': '',
        }])
        out = await engine.scan(page)
        node = out[0]['nodes'][0]
        assert (node['target'][0]
                == 'WCAG2AA.Principle3.Guideline3_1.3_1_1.H57.2')

    async def test_evaluate_exception_returns_empty(self, engine):
        engine.verbose = True
        engine.quiet = False
        page = _MockPage()
        page.set_evaluate_raises(RuntimeError('boom'))
        out = await engine.scan(page)
        assert out == []
