"""
Engine rule mappings and tag taxonomy for a11y-catscan.

Three-tier tag system:

    Every finding from every engine is tagged with at least one of:

    wcag-X.Y.Z   (87 tags)  WCAG Success Criteria — compliance failures.
                             One tag per SC, e.g. wcag-1.4.3 for Contrast.
                             Source: SC_META dict below.

    aria-*        (6 tags)   WAI-ARIA conformance — spec violations.
                             valid-attrs, valid-roles, required-structure,
                             naming, hidden, required-states.
                             Source: ARIA_CATEGORIES and *_ARIA_MAP dicts.

    bp-*         (10 tags)   Best practices — engine recommendations.
                             landmarks, headings, keyboard, forms, tables,
                             images, color, viewport, scripting, testability.
                             Source: BP_CATEGORIES and *_BP_MAP dicts.

    A finding can carry tags from multiple tiers.  For example, an
    invalid aria-valuenow is both wcag-4.1.2 and aria-valid-attrs.
    Filtering by WCAG shows it under 4.1.2; filtering by ARIA shows
    it under aria-valid-attrs.

Engine mapping sources:

    IBM Equal Access: IBM_SC_MAP below (158 rules → WCAG SCs).
        Upstream: https://github.com/IBMa/equal-access
        Package: accessibility-checker-engine (npm)

    Siteimprove Alfa: WCAG SCs declared at runtime + ALFA_RULES in
        engines/alfa.py fills gaps where Alfa metadata is missing.
        Upstream: https://github.com/Siteimprove/alfa

    axe-core: WCAG tags normalized from axe's native format
        (wcag143 → wcag-1.4.3) in engines/axe.py.
        Upstream: https://github.com/dequelabs/axe-core

    HTML_CodeSniffer: SC extracted from rule codes
        (WCAG2AA.Principle1.Guideline1_4.1_4_3 → 1.4.3).
        Upstream: https://github.com/nickersk/HTML_CodeSniffer

To validate IBM mappings: python3 engine_mappings.py --check

Last updated: 2026-04-21
"""

import re

# ── EARL outcomes (W3C standard) ────────────────────────────────
#
# The W3C Evaluation and Report Language (EARL 1.0) defines four
# canonical test outcomes.  All major accessibility engines use these
# internally (axe-core RawNodeResult, Alfa Outcome.Value, IBM ACT
# mappings).  We use EARL terms as the internal representation and
# translate to user-friendly names at the report/display layer.
#
# Reference: https://www.w3.org/TR/EARL10-Schema/#outcome

EARL_FAILED = 'failed'           # Definite accessibility failure
EARL_CANTTELL = 'cantTell'       # Needs manual review
EARL_PASSED = 'passed'           # Test passed
EARL_INAPPLICABLE = 'inapplicable'  # Test does not apply

# EARL → user-friendly display names (CLI output, HTML reports)
EARL_TO_DISPLAY = {
    EARL_FAILED: 'failed',
    EARL_CANTTELL: "can't tell",
    EARL_PASSED: 'passed',
    EARL_INAPPLICABLE: 'inapplicable',
}


# WCAG Success Criteria — complete inventory.
# Each entry: 'X.Y.Z': (level, version_introduced, official_name)
# Names are from the W3C WCAG spec headings.
# Reference: https://www.w3.org/TR/WCAG22/
SC_META = {
    # 1. Perceivable
    '1.1.1': ('A', '2.0', 'Non-text Content'),
    '1.2.1': ('A', '2.0', 'Audio-only and Video-only (Prerecorded)'),
    '1.2.2': ('A', '2.0', 'Captions (Prerecorded)'),
    '1.2.3': ('A', '2.0', 'Audio Description or Media Alternative (Prerecorded)'),
    '1.2.4': ('AA', '2.0', 'Captions (Live)'),
    '1.2.5': ('AA', '2.0', 'Audio Description (Prerecorded)'),
    '1.2.6': ('AAA', '2.0', 'Sign Language (Prerecorded)'),
    '1.2.7': ('AAA', '2.0', 'Extended Audio Description (Prerecorded)'),
    '1.2.8': ('AAA', '2.0', 'Media Alternative (Prerecorded)'),
    '1.2.9': ('AAA', '2.0', 'Audio-only (Live)'),
    '1.3.1': ('A', '2.0', 'Info and Relationships'),
    '1.3.2': ('A', '2.0', 'Meaningful Sequence'),
    '1.3.3': ('A', '2.0', 'Sensory Characteristics'),
    '1.3.4': ('AA', '2.1', 'Orientation'),
    '1.3.5': ('AA', '2.1', 'Identify Input Purpose'),
    '1.3.6': ('AAA', '2.1', 'Identify Purpose'),
    '1.4.1': ('A', '2.0', 'Use of Color'),
    '1.4.2': ('A', '2.0', 'Audio Control'),
    '1.4.3': ('AA', '2.0', 'Contrast (Minimum)'),
    '1.4.4': ('AA', '2.0', 'Resize Text'),
    '1.4.5': ('AA', '2.0', 'Images of Text'),
    '1.4.6': ('AAA', '2.0', 'Contrast (Enhanced)'),
    '1.4.7': ('AAA', '2.0', 'Low or No Background Audio'),
    '1.4.8': ('AAA', '2.0', 'Visual Presentation'),
    '1.4.9': ('AAA', '2.0', 'Images of Text (No Exception)'),
    '1.4.10': ('AA', '2.1', 'Reflow'),
    '1.4.11': ('AA', '2.1', 'Non-text Contrast'),
    '1.4.12': ('AA', '2.1', 'Text Spacing'),
    '1.4.13': ('AA', '2.1', 'Content on Hover or Focus'),
    # 2. Operable
    '2.1.1': ('A', '2.0', 'Keyboard'),
    '2.1.2': ('A', '2.0', 'No Keyboard Trap'),
    '2.1.3': ('AAA', '2.0', 'Keyboard (No Exception)'),
    '2.1.4': ('A', '2.1', 'Character Key Shortcuts'),
    '2.2.1': ('A', '2.0', 'Timing Adjustable'),
    '2.2.2': ('A', '2.0', 'Pause, Stop, Hide'),
    '2.2.3': ('AAA', '2.0', 'No Timing'),
    '2.2.4': ('AAA', '2.0', 'Interruptions'),
    '2.2.5': ('AAA', '2.0', 'Re-authenticating'),
    '2.2.6': ('AAA', '2.1', 'Timeouts'),
    '2.3.1': ('A', '2.0', 'Three Flashes or Below Threshold'),
    '2.3.2': ('AAA', '2.0', 'Three Flashes'),
    '2.3.3': ('AAA', '2.0', 'Animation from Interactions'),
    '2.4.1': ('A', '2.0', 'Bypass Blocks'),
    '2.4.2': ('A', '2.0', 'Page Titled'),
    '2.4.3': ('A', '2.0', 'Focus Order'),
    '2.4.4': ('A', '2.0', 'Link Purpose (In Context)'),
    '2.4.5': ('AA', '2.0', 'Multiple Ways'),
    '2.4.6': ('AA', '2.0', 'Headings and Labels'),
    '2.4.7': ('AA', '2.0', 'Focus Visible'),
    '2.4.8': ('AAA', '2.0', 'Location'),
    '2.4.9': ('AAA', '2.0', 'Link Purpose (Link Only)'),
    '2.4.10': ('AAA', '2.0', 'Section Headings'),
    '2.4.11': ('AA', '2.2', 'Focus Not Obscured (Minimum)'),
    '2.4.12': ('AAA', '2.2', 'Focus Not Obscured (Enhanced)'),
    '2.4.13': ('AAA', '2.2', 'Focus Appearance'),
    '2.5.1': ('A', '2.1', 'Pointer Gestures'),
    '2.5.2': ('A', '2.1', 'Pointer Cancellation'),
    '2.5.3': ('A', '2.1', 'Label in Name'),
    '2.5.4': ('A', '2.1', 'Motion Actuation'),
    '2.5.5': ('AAA', '2.1', 'Target Size (Enhanced)'),
    '2.5.6': ('AAA', '2.1', 'Concurrent Input Mechanisms'),
    '2.5.7': ('AA', '2.2', 'Dragging Movements'),
    '2.5.8': ('AA', '2.2', 'Target Size (Minimum)'),
    # 3. Understandable
    '3.1.1': ('A', '2.0', 'Language of Page'),
    '3.1.2': ('AA', '2.0', 'Language of Parts'),
    '3.1.3': ('AAA', '2.0', 'Unusual Words'),
    '3.1.4': ('AAA', '2.0', 'Abbreviations'),
    '3.1.5': ('AAA', '2.0', 'Reading Level'),
    '3.1.6': ('AAA', '2.0', 'Pronunciation'),
    '3.2.1': ('A', '2.0', 'On Focus'),
    '3.2.2': ('A', '2.0', 'On Input'),
    '3.2.3': ('AA', '2.0', 'Consistent Navigation'),
    '3.2.4': ('AA', '2.0', 'Consistent Identification'),
    '3.2.5': ('AAA', '2.0', 'Change on Request'),
    '3.2.6': ('A', '2.2', 'Consistent Help'),
    '3.3.1': ('A', '2.0', 'Error Identification'),
    '3.3.2': ('A', '2.0', 'Labels or Instructions'),
    '3.3.3': ('AA', '2.0', 'Error Suggestion'),
    '3.3.4': ('AA', '2.0', 'Error Prevention (Legal, Financial, Data)'),
    '3.3.5': ('AAA', '2.0', 'Help'),
    '3.3.6': ('AAA', '2.0', 'Error Prevention (All)'),
    '3.3.7': ('A', '2.2', 'Redundant Entry'),
    '3.3.8': ('AA', '2.2', 'Accessible Authentication (Minimum)'),
    '3.3.9': ('A', '2.2', 'Accessible Authentication (Enhanced)'),
    # 4. Robust
    '4.1.1': ('A', '2.0', 'Parsing'),
    '4.1.2': ('A', '2.0', 'Name, Role, Value'),
    '4.1.3': ('AA', '2.1', 'Status Messages'),
}


def sc_level(sc):
    """Return (level, version) for a WCAG SC, e.g. ('AA', '2.1')."""
    meta = SC_META.get(sc)
    if meta:
        return (meta[0], meta[1])
    return ('?', '?')


def sc_name(sc):
    """Return the official W3C name for a WCAG SC, e.g. 'Contrast (Minimum)'."""
    meta = SC_META.get(sc)
    return meta[2] if meta else ''


# IBM Equal Access rule ID → WCAG SC(s).
# Extracted from ace.js 4.0.16 bundled rule definitions (num: field).
# Rules not in this map are engine-specific best practices.
IBM_SC_MAP = {
    'a_target_warning': ['3.2.2'],
    'a_text_purpose': ['2.4.4', '4.1.2'],
    'applet_alt_exists': ['1.1.1'],
    'application_content_accessible': ['1.1.1', '2.1.1'],
    'area_alt_exists': ['1.1.1'],
    'aria_accessiblename_exists': ['4.1.2'],
    'aria_activedescendant_tabindex_valid': ['2.1.1'],
    'aria_activedescendant_valid': ['4.1.2'],
    'aria_application_label_unique': ['2.4.1'],
    'aria_application_labelled': ['2.4.1'],
    'aria_article_label_unique': ['2.4.1'],
    'aria_attribute_allowed': ['4.1.2'],
    'aria_attribute_conflict': ['4.1.2'],
    'aria_attribute_exists': ['4.1.2'],
    'aria_attribute_redundant': ['4.1.2'],
    'aria_attribute_required': ['4.1.2'],
    'aria_attribute_value_valid': ['4.1.2'],
    'aria_banner_label_unique': ['2.4.1'],
    'aria_banner_single': ['2.4.1'],
    'aria_child_tabbable': ['2.1.1'],
    'aria_child_valid': ['1.3.1'],
    'aria_complementary_label_unique': ['2.4.1'],
    'aria_complementary_label_visible': ['2.4.1'],
    'aria_complementary_labelled': ['2.4.1'],
    'aria_content_in_landmark': ['2.4.1'],
    'aria_contentinfo_label_unique': ['2.4.1'],
    'aria_contentinfo_misuse': ['2.4.1'],
    'aria_contentinfo_single': ['2.4.1'],
    'aria_descendant_valid': ['4.1.2'],
    'aria_document_label_unique': ['2.4.1'],
    'aria_eventhandler_role_valid': ['4.1.2'],
    'aria_form_label_unique': ['2.4.1'],
    'aria_graphic_labelled': ['1.3.1', '4.1.2'],
    'aria_id_unique': ['4.1.2'],
    'aria_img_labelled': ['2.1.1'],
    'aria_landmark_name_unique': ['2.4.1'],
    'aria_main_label_unique': ['2.4.1'],
    'aria_main_label_visible': ['2.4.1'],
    'aria_navigation_label_unique': ['2.4.1'],
    'aria_parent_required': ['1.3.1'],
    'aria_region_label_unique': ['2.4.1'],
    'aria_region_labelled': ['2.4.1'],
    'aria_role_allowed': ['4.1.2'],
    'aria_role_valid': ['4.1.2'],
    'aria_search_label_unique': ['2.4.1'],
    'aria_toolbar_label_unique': ['2.4.1'],
    'aria_widget_labelled': ['4.1.2'],
    'asciiart_alt_exists': ['2.2.2'],
    'blink_elem_deprecated': ['2.2.2'],
    'blockquote_cite_exists': ['1.3.1'],
    'canvas_content_described': ['1.1.1', '2.1.1', '4.1.2'],
    'caption_track_exists': ['1.2.2'],
    'combobox_active_descendant': ['4.1.2'],
    'combobox_autocomplete_valid': ['4.1.2'],
    'combobox_design_valid': ['4.1.2'],
    'combobox_focusable_elements': ['4.1.2'],
    'combobox_haspopup_valid': ['4.1.2'],
    'combobox_popup_reference': ['4.1.2'],
    'debug_paths': ['1.3.2'],
    'download_keyboard_controllable': ['2.1.2'],
    'draggable_alternative_exists': ['2.5.7'],
    'element_accesskey_labelled': ['3.3.2'],
    'element_lang_valid': ['3.1.2'],
    'element_mouseevent_keyboard': ['2.1.1'],
    'element_orientation_unlocked': ['1.3.4'],
    'element_scrollable_tabbable': ['4.1.2'],
    'element_tabbable_unobscured': ['2.4.11'],
    'element_tabbable_visible': ['2.4.7'],
    'embed_alt_exists': ['1.1.1'],
    'emoticons_alt_exists': ['3.3.1'],
    'fieldset_label_valid': ['1.3.1', '3.3.2'],
    'fieldset_legend_valid': ['1.3.1'],
    'figure_label_exists': ['1.1.1'],
    'form_font_color': ['1.4.1'],
    'form_interaction_review': ['3.2.2'],
    'form_label_unique': ['1.3.1'],
    'form_submit_button_exists': ['3.2.2'],
    'form_submit_review': ['2.4.1'],
    'frame_title_exists': ['4.1.2'],
    'heading_content_exists': ['2.4.6'],
    'heading_markup_misuse': ['1.3.1'],
    'html_lang_exists': ['3.1.1'],
    'html_lang_valid': ['3.1.1'],
    'html_skipnav_exists': ['2.4.1'],
    'iframe_interactive_tabbable': ['2.1.1'],
    'imagebutton_alt_exists': ['1.1.1'],
    'imagemap_alt_exists': ['1.1.1'],
    'img_alt_background': ['1.1.1'],
    'img_alt_decorative': ['1.1.1'],
    'img_alt_misuse': ['1.1.1'],
    'img_alt_null': ['1.1.1'],
    'img_alt_redundant': ['1.1.1', '2.4.4'],
    'img_alt_valid': ['1.1.1'],
    'img_ismap_misuse': ['1.1.1'],
    'img_longdesc_misuse': ['1.1.1'],
    'input_autocomplete_valid': ['1.3.5'],
    'input_fields_grouped': ['1.3.1'],
    'input_haspopup_conflict': ['4.1.2'],
    'input_label_after': ['3.3.2'],
    'input_label_before': ['3.3.2'],
    'input_label_exists': ['4.1.2'],
    'input_label_visible': ['2.5.3', '3.3.2'],
    'input_onchange_review': ['3.2.2'],
    'input_placeholder_label_visible': ['4.1.2'],
    'label_name_visible': ['2.5.3'],
    'label_ref_valid': ['1.3.1'],
    'list_children_valid': ['4.1.2'],
    'list_markup_review': ['1.3.1'],
    'list_structure_proper': ['1.3.1'],
    'marquee_elem_avoid': ['2.2.2'],
    'media_alt_brief': ['1.1.1'],
    'media_alt_exists': ['1.1.1'],
    'media_audio_transcribed': ['1.2.1'],
    'media_autostart_controllable': ['1.4.2'],
    'media_keyboard_controllable': ['2.1.1'],
    'media_live_captioned': ['1.2.4'],
    'media_track_available': ['1.2.3', '1.2.5'],
    'meta_redirect_optional': ['2.2.4', '3.2.5'],
    'meta_refresh_delay': ['2.2.1'],
    'meta_viewport_zoomable': ['1.4.4'],
    'noembed_content_exists': ['1.1.1'],
    'object_text_exists': ['1.1.1'],
    'page_title_exists': ['2.4.2'],
    'page_title_valid': ['2.4.2'],
    'script_focus_blur_review': ['2.1.1', '2.4.7', '3.2.1'],
    'script_onclick_avoid': ['2.1.1'],
    'script_onclick_misuse': ['2.1.1'],
    'script_select_review': ['3.2.1'],
    'select_options_grouped': ['1.3.1'],
    'skip_main_described': ['2.4.1'],
    'skip_main_exists': ['2.4.1'],
    'style_background_decorative': ['1.1.1'],
    'style_before_after_review': ['1.3.1'],
    'style_color_misuse': ['1.4.1'],
    'style_focus_visible': ['2.4.7'],
    'style_highcontrast_visible': ['1.1.1', '1.3.2', '1.4.11'],
    'style_hover_persistent': ['1.4.13'],
    'style_viewport_resizable': ['1.4.4', '1.4.10'],
    'svg_graphics_labelled': ['1.1.1'],
    'table_aria_descendants': ['4.1.2'],
    'table_caption_empty': ['1.3.1'],
    'table_caption_nested': ['1.3.1'],
    'table_headers_exists': ['1.3.1'],
    'table_headers_ref_valid': ['1.3.1'],
    'table_headers_related': ['1.3.1'],
    'table_layout_linearized': ['1.3.1'],
    'table_scope_valid': ['1.3.1'],
    'table_structure_misuse': ['1.3.1'],
    'table_summary_redundant': ['1.3.1'],
    'target_spacing_sufficient': ['2.5.8'],
    'text_block_heading': ['1.3.1'],
    'text_contrast_sufficient': ['1.4.3'],
    'text_quoted_correctly': ['1.3.1'],
    'text_sensory_misuse': ['1.3.3'],
    'text_spacing_valid': ['1.4.12'],
    'text_whitespace_valid': ['1.3.2'],
    'widget_tabbable_exists': ['2.1.1'],
    'widget_tabbable_single': ['2.1.1', '2.4.3'],
}


def ibm_rule_to_sc(rule_id):
    """Map an IBM rule ID to its WCAG SC(s). Returns [] for best practices."""
    return IBM_SC_MAP.get(rule_id, [])


def ibm_rule_to_tags(rule_id):
    """Map an IBM rule ID to normalized WCAG tags (wcag-X.Y.Z format)."""
    scs = ibm_rule_to_sc(rule_id)
    return ['wcag-' + sc for sc in scs]


def htmlcs_code_to_sc(code):
    """Extract WCAG SC from HTML_CodeSniffer code.

    E.g. 'WCAG2AA.Principle1.Guideline1_4.1_4_3.G18' → '1.4.3'
    """
    m = re.search(r'(\d+)_(\d+)_(\d+)', code)
    if m:
        return '{}.{}.{}'.format(m.group(1), m.group(2), m.group(3))
    return None


# ── Best practice categories ────────────────────────────────────
#
# Rules that don't map to a specific WCAG Success Criterion but still
# represent widely-agreed accessibility best practices.  Only axe-core
# and IBM Equal Access have such rules; HTMLCS and Alfa are purely
# WCAG-derived.
#
# The canonical categories are based on axe-core's `cat.*` tags with
# IBM RECOMMENDATION rules mapped into the same taxonomy.  Each rule
# maps to exactly one category.
#
# Sources:
#   axe-core 4.11.3: rules tagged 'best-practice' (30 rules)
#   IBM Equal Access 4.0.16: rules that produce 'RECOMMENDATION' results
#     Note: IBM categorization is per-result, not per-rule.  A rule can
#     produce VIOLATION on one element and RECOMMENDATION on another.
#     The rules listed here are those that *can* produce RECOMMENDATION.
#
# Last updated: 2026-04-20

# Category descriptions — what each best-practice category covers.
BP_CATEGORIES = {
    'landmarks': 'Page structure: content in landmarks, landmark hierarchy '
                 'and uniqueness, required landmarks (main, banner, etc.)',
    'headings': 'Heading hierarchy: order, presence of h1, non-empty '
                'headings, heading vs bold misuse',
    'keyboard': 'Keyboard access beyond WCAG: accesskey uniqueness, '
                'focus order semantics, tabindex values, skip links',
    'forms': 'Form usability: visible labels (not title-only), field '
             'grouping, select option grouping, legend validity',
    'tables': 'Table semantics: scope attributes, caption/summary '
              'duplication, empty headers, layout table linearization',
    'images': 'Image alt text quality: redundant alt, background images, '
              'alt text brevity for media',
    'color': 'Color and contrast beyond WCAG: high-contrast mode '
             'visibility, color as sole indicator',
    'viewport': 'Viewport scaling: meta viewport allows significant zoom',
    'scripting': 'Script accessibility: mouse events with keyboard '
                 'equivalents, onclick misuse, target=_blank warnings',
    'testability': 'Scanner limitations: hidden content, untested frames',
}

# axe-core best-practice rules → category
AXE_BP_MAP = {
    # Landmarks
    'region':                              'landmarks',
    'landmark-one-main':                   'landmarks',
    'landmark-unique':                     'landmarks',
    'landmark-banner-is-top-level':        'landmarks',
    'landmark-main-is-top-level':          'landmarks',
    'landmark-contentinfo-is-top-level':   'landmarks',
    'landmark-complementary-is-top-level': 'landmarks',
    'landmark-no-duplicate-banner':        'landmarks',
    'landmark-no-duplicate-main':          'landmarks',
    'landmark-no-duplicate-contentinfo':   'landmarks',
    # Headings
    'heading-order':                       'headings',
    'page-has-heading-one':                'headings',
    'empty-heading':                       'headings',
    'empty-table-header':                  'headings',
    # Keyboard
    'accesskeys':                          'keyboard',
    'focus-order-semantics':               'keyboard',
    'skip-link':                           'keyboard',
    'tabindex':                            'keyboard',
    # Forms
    'label-title-only':                    'forms',
    # Tables
    'scope-attr-valid':                    'tables',
    'table-duplicate-name':                'tables',
    # Images
    'image-redundant-alt':                 'images',
    # Viewport
    'meta-viewport-large':                 'viewport',
    # Testability
    'frame-tested':                        'testability',
    'hidden-content':                      'testability',
}

# IBM RECOMMENDATION rules → category.
# These rules CAN produce RECOMMENDATION-category results (as opposed
# to VIOLATION).  The engine labels each finding at runtime; this map
# lets us categorize the non-WCAG findings they produce.
IBM_BP_MAP = {
    # Landmarks
    'aria_content_in_landmark':            'landmarks',
    'aria_contentinfo_misuse':             'landmarks',
    # Headings
    'heading_content_exists':              'headings',
    # Forms
    'input_fields_grouped':               'forms',
    'select_options_grouped':              'forms',
    'fieldset_legend_valid':               'forms',
    # Tables
    'table_layout_linearized':            'tables',
    # Images
    'img_alt_background':                 'images',
    'media_alt_brief':                    'images',
    # Color
    'style_highcontrast_visible':         'color',
    # Scripting
    'element_mouseevent_keyboard':        'scripting',
    'script_onclick_avoid':               'scripting',
    'a_target_warning':                   'scripting',
}


# ── ARIA conformance categories ─────────────────────────────────
#
# WAI-ARIA is a W3C specification separate from WCAG.  WCAG references
# it (primarily from SC 4.1.2), so ARIA violations are implicit WCAG
# failures — but they're a distinct category worth tracking separately.
#
# A finding can carry BOTH a WCAG SC tag AND an aria-* tag.  For
# example, "invalid aria-valuenow" is both WCAG 4.1.2 and
# aria-valid-attrs.  Filtering by WCAG shows it under 4.1.2;
# filtering by ARIA shows it under aria-valid-attrs.
#
# Reference: https://www.w3.org/TR/wai-aria-1.2/

ARIA_CATEGORIES = {
    'valid-attrs': 'ARIA attribute validity: attributes exist, values '
                        'are valid, attributes allowed/not prohibited for role',
    'valid-roles': 'ARIA role validity: role values exist, not '
                        'deprecated, appropriate for the element',
    'required-structure': 'ARIA document structure: required owned '
                               'elements (children), required context roles '
                               '(parents), valid descendants',
    'naming': 'ARIA accessible names: buttons, inputs, toggles, '
                   'meters, progressbars, dialogs, treeitems have names',
    'hidden': 'aria-hidden correctness: not on body, hidden '
                   'elements and their children not focusable',
    'required-states': 'Required ARIA states and properties: '
                            'required attributes present for the role',
}

# Cross-engine ARIA rule → aria-* category mappings.
# Rules listed here get an 'aria-<category>' tag on their results,
# in addition to any WCAG SC tags they already carry.
AXE_ARIA_MAP = {
    # aria-valid-attrs
    'aria-valid-attr':          'valid-attrs',
    'aria-valid-attr-value':    'valid-attrs',
    'aria-allowed-attr':        'valid-attrs',
    'aria-prohibited-attr':     'valid-attrs',
    'aria-conditional-attr':    'valid-attrs',
    'aria-braille-equivalent':  'valid-attrs',
    # aria-valid-roles
    'aria-roles':               'valid-roles',
    'aria-deprecated-role':     'valid-roles',
    'aria-allowed-role':        'valid-roles',
    'aria-roledescription':     'valid-roles',
    'presentation-role-conflict': 'valid-roles',
    # aria-required-structure
    'aria-required-children':   'required-structure',
    'aria-required-parent':     'required-structure',
    # aria-naming
    'aria-command-name':        'naming',
    'aria-input-field-name':    'naming',
    'aria-toggle-field-name':   'naming',
    'aria-meter-name':          'naming',
    'aria-progressbar-name':    'naming',
    'aria-tooltip-name':        'naming',
    'aria-dialog-name':         'naming',
    'aria-treeitem-name':       'naming',
    'aria-text':                'naming',
    # aria-hidden
    'aria-hidden-body':         'hidden',
    'aria-hidden-focus':        'hidden',
    # aria-required-states
    'aria-required-attr':       'required-states',
}

IBM_ARIA_MAP = {
    # aria-valid-attrs
    'aria_attribute_exists':        'valid-attrs',
    'aria_attribute_value_valid':   'valid-attrs',
    'aria_attribute_allowed':       'valid-attrs',
    'aria_attribute_conflict':      'valid-attrs',
    'aria_attribute_redundant':     'valid-attrs',
    # aria-valid-roles
    'aria_role_valid':              'valid-roles',
    'aria_role_allowed':            'valid-roles',
    'element_tabbable_role_valid':  'valid-roles',
    # aria-required-structure
    'aria_child_valid':             'required-structure',
    'aria_parent_required':         'required-structure',
    'aria_descendant_valid':        'required-structure',
    'aria_child_tabbable':          'required-structure',
    # aria-naming
    'aria_widget_labelled':         'naming',
    'aria_accessiblename_exists':   'naming',
    'aria_graphic_labelled':        'naming',
    'aria_img_labelled':            'naming',
    # aria-required-states
    'aria_attribute_required':      'required-states',
}

ALFA_ARIA_MAP = {
    # aria-valid-attrs
    'sia-r18':  'valid-attrs',
    'sia-r19':  'valid-attrs',
    # aria-valid-roles
    'sia-r21':  'valid-roles',
    'sia-r70':  'valid-roles',
    # aria-required-structure
    'sia-r22':  'required-structure',
    'sia-r23':  'required-structure',
    'sia-r64':  'required-structure',
    'sia-r110': 'required-structure',
    # aria-hidden
    'sia-r60':  'hidden',
    'sia-r86':  'hidden',
    # aria-required-states
    'sia-r20':  'required-states',
    'sia-r90':  'required-states',
}

# Also map Alfa's remaining unmapped rules that aren't ARIA but
# are best practices.  These carry bp-* tags.
ALFA_BP_MAP = {
    'sia-r48':  'landmarks',   # deprecated element (structural)
    'sia-r49':  'landmarks',   # deprecated attribute (structural)
    'sia-r53':  'headings',    # heading is descriptive
    'sia-r54':  'landmarks',   # landmark has unique role
    'sia-r55':  'landmarks',   # landmark visible role
    'sia-r56':  'landmarks',   # landmark is top-level
    'sia-r57':  'landmarks',   # content in landmark / contrast candidate
    'sia-r59':  'landmarks',   # body has main landmark
    'sia-r61':  'landmarks',   # document has one main landmark
    'sia-r72':  'viewport',    # paragraph max width
    'sia-r75':  'viewport',    # font size >= 9px
    'sia-r78':  'keyboard',    # no positive tabindex
    'sia-r79':  'viewport',    # element not clipped
    'sia-r85':  'keyboard',    # scrollable region focusable
    'sia-r87':  'viewport',    # no first-child letter exception
}


def aria_category(engine, rule_id):
    """Return the ARIA conformance category for a rule, or None.

    A rule can have BOTH a WCAG SC and an ARIA category — they are
    not mutually exclusive.  For example, axe's 'aria-valid-attr' is
    both WCAG 4.1.2 and aria-valid-attrs.
    """
    if engine == 'axe':
        return AXE_ARIA_MAP.get(rule_id)
    if engine == 'ibm':
        return IBM_ARIA_MAP.get(rule_id)
    if engine == 'alfa':
        return ALFA_ARIA_MAP.get(rule_id)
    return None


def bp_category(engine, rule_id):
    """Return the best-practice category for a non-WCAG rule, or None.

    Args:
        engine: 'axe', 'ibm', 'alfa', or 'htmlcs'
        rule_id: the engine-specific rule identifier

    Returns:
        Category string (e.g. 'landmarks') or None if the rule is
        WCAG-mapped or not recognized as a best practice.
    """
    if engine == 'axe':
        return AXE_BP_MAP.get(rule_id)
    if engine == 'ibm':
        return IBM_BP_MAP.get(rule_id)
    if engine == 'alfa':
        return ALFA_BP_MAP.get(rule_id)
    return None


# --- Validation ---

if __name__ == '__main__':
    import sys
    if '--check' in sys.argv:
        # Compare hardcoded IBM mapping against live ace.js
        import os
        ace_path = os.path.join(os.path.dirname(__file__),
            'node_modules', 'accessibility-checker-engine', 'ace.js')
        if not os.path.exists(ace_path):
            print("ace.js not found — run npm install")
            sys.exit(1)
        src = open(ace_path).read()
        live = {}
        for m in re.finditer(
                r'id:\s*"(\w+)".*?(?:num:\s*"(\d+\.\d+\.\d+)"'
                r'|num:\s*\[([^\]]+)\])', src, re.DOTALL):
            rule_id = m.group(1)
            if m.group(2):
                live[rule_id] = [m.group(2)]
            elif m.group(3):
                nums = re.findall(r'"(\d+\.\d+\.\d+)"', m.group(3))
                if nums:
                    live[rule_id] = nums

        new_rules = set(live.keys()) - set(IBM_SC_MAP.keys())
        removed = set(IBM_SC_MAP.keys()) - set(live.keys())
        changed = {r for r in live if r in IBM_SC_MAP
                    and sorted(live[r]) != sorted(IBM_SC_MAP[r])}

        if not new_rules and not removed and not changed:
            print("IBM mapping is up to date ({} rules)".format(
                len(IBM_SC_MAP)))
        else:
            if new_rules:
                print("NEW rules (add to IBM_SC_MAP):")
                for r in sorted(new_rules):
                    print("  '{}': {},".format(r, live[r]))
            if removed:
                print("REMOVED rules (delete from IBM_SC_MAP):")
                for r in sorted(removed):
                    print("  '{}'".format(r))
            if changed:
                print("CHANGED rules:")
                for r in sorted(changed):
                    print("  '{}': {} → {}".format(
                        r, IBM_SC_MAP[r], live[r]))
            sys.exit(1)
    else:
        print("Usage: python3 engine_mappings.py --check")
        print("Validates IBM_SC_MAP against installed ace.js")
