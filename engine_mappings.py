"""
Engine rule → WCAG SC mappings for a11y-catscan.

Each engine has its own mapping from rule IDs to WCAG Success Criteria.
This lets us categorize findings by WCAG level (A/AA/AAA) and version
(2.0/2.1/2.2) regardless of which engine found them.

Sources:
  - IBM Equal Access: extracted from ace.js bundled rule definitions.
    Upstream: https://github.com/IBMa/equal-access
    Rule docs: https://www.ibm.com/able/requirements/checker-rule-sets
    Package: accessibility-checker-engine (npm)

  - Siteimprove Alfa: each rule declares its WCAG requirements at runtime.
    The mapping is extracted live when rules are loaded, not hardcoded.
    Upstream: https://github.com/Siteimprove/alfa

  - axe-core: rules have WCAG tags (e.g. 'wcag143' = SC 1.4.3).
    Extracted at runtime from result tags. Not hardcoded.
    Upstream: https://github.com/dequelabs/axe-core

  - HTML_CodeSniffer: rule codes contain the SC
    (e.g. 'WCAG2AA.Principle1.Guideline1_4.1_4_3'). Extracted at runtime.
    Upstream: https://github.com/nickersk/HTML_CodeSniffer

To update: run `python3 engine_mappings.py --check` which loads the
current ace.js and compares against the hardcoded mapping, reporting
any new or removed rules.

Last updated: 2026-04-20 from accessibility-checker-engine 4.0.16
"""

import re

# WCAG SC → (level, version_introduced)
SC_META = {
    # Perceivable
    '1.1.1': ('A', '2.0'),
    '1.2.1': ('A', '2.0'), '1.2.2': ('A', '2.0'), '1.2.3': ('A', '2.0'),
    '1.2.4': ('AA', '2.0'), '1.2.5': ('AA', '2.0'),
    '1.2.6': ('AAA', '2.0'), '1.2.7': ('AAA', '2.0'),
    '1.2.8': ('AAA', '2.0'), '1.2.9': ('AAA', '2.0'),
    '1.3.1': ('A', '2.0'), '1.3.2': ('A', '2.0'), '1.3.3': ('A', '2.0'),
    '1.3.4': ('AA', '2.1'), '1.3.5': ('AA', '2.1'), '1.3.6': ('AAA', '2.1'),
    '1.4.1': ('A', '2.0'), '1.4.2': ('A', '2.0'),
    '1.4.3': ('AA', '2.0'), '1.4.4': ('AA', '2.0'), '1.4.5': ('AA', '2.0'),
    '1.4.6': ('AAA', '2.0'), '1.4.7': ('AAA', '2.0'),
    '1.4.8': ('AAA', '2.0'), '1.4.9': ('AAA', '2.0'),
    '1.4.10': ('AA', '2.1'), '1.4.11': ('AA', '2.1'),
    '1.4.12': ('AA', '2.1'), '1.4.13': ('AA', '2.1'),
    # Operable
    '2.1.1': ('A', '2.0'), '2.1.2': ('A', '2.0'),
    '2.1.3': ('AAA', '2.0'), '2.1.4': ('A', '2.1'),
    '2.2.1': ('A', '2.0'), '2.2.2': ('A', '2.0'),
    '2.2.3': ('AAA', '2.0'), '2.2.4': ('AAA', '2.0'),
    '2.2.5': ('AAA', '2.0'), '2.2.6': ('AAA', '2.1'),
    '2.3.1': ('A', '2.0'), '2.3.2': ('AAA', '2.0'), '2.3.3': ('AAA', '2.0'),
    '2.4.1': ('A', '2.0'), '2.4.2': ('A', '2.0'), '2.4.3': ('A', '2.0'),
    '2.4.4': ('A', '2.0'), '2.4.5': ('AA', '2.0'), '2.4.6': ('AA', '2.0'),
    '2.4.7': ('AA', '2.0'), '2.4.8': ('AAA', '2.0'),
    '2.4.9': ('AAA', '2.0'), '2.4.10': ('AAA', '2.0'),
    '2.4.11': ('AA', '2.2'), '2.4.12': ('AAA', '2.2'),
    '2.4.13': ('AAA', '2.2'),
    '2.5.1': ('A', '2.1'), '2.5.2': ('A', '2.1'),
    '2.5.3': ('A', '2.1'), '2.5.4': ('A', '2.1'),
    '2.5.5': ('AAA', '2.1'), '2.5.6': ('AAA', '2.1'),
    '2.5.7': ('AA', '2.2'), '2.5.8': ('AA', '2.2'),
    # Understandable
    '3.1.1': ('A', '2.0'), '3.1.2': ('AA', '2.0'),
    '3.1.3': ('AAA', '2.0'), '3.1.4': ('AAA', '2.0'),
    '3.1.5': ('AAA', '2.0'), '3.1.6': ('AAA', '2.0'),
    '3.2.1': ('A', '2.0'), '3.2.2': ('A', '2.0'),
    '3.2.3': ('AA', '2.0'), '3.2.4': ('AA', '2.0'),
    '3.2.5': ('AAA', '2.0'), '3.2.6': ('A', '2.2'),
    '3.3.1': ('A', '2.0'), '3.3.2': ('A', '2.0'),
    '3.3.3': ('AA', '2.0'), '3.3.4': ('AA', '2.0'),
    '3.3.5': ('AAA', '2.0'), '3.3.6': ('AAA', '2.0'),
    '3.3.7': ('A', '2.2'), '3.3.8': ('AA', '2.2'), '3.3.9': ('A', '2.2'),
    # Robust
    '4.1.1': ('A', '2.0'), '4.1.2': ('A', '2.0'), '4.1.3': ('AA', '2.1'),
}


def sc_level(sc):
    """Return (level, version) for a WCAG SC, e.g. ('AA', '2.1')."""
    return SC_META.get(sc, ('?', '?'))


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
    """Map an IBM rule ID to axe-style WCAG tags for grouping."""
    scs = ibm_rule_to_sc(rule_id)
    return ['wcag' + sc.replace('.', '') for sc in scs]


def htmlcs_code_to_sc(code):
    """Extract WCAG SC from HTML_CodeSniffer code.

    E.g. 'WCAG2AA.Principle1.Guideline1_4.1_4_3.G18' → '1.4.3'
    """
    m = re.search(r'(\d+)_(\d+)_(\d+)', code)
    if m:
        return '{}.{}.{}'.format(m.group(1), m.group(2), m.group(3))
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
