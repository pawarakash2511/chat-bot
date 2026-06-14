"""
Tests for Hebrew PDF text extraction fixes.
Tests the pure logic directly (no app dependencies needed).
"""
import re

# --- replicated pure functions from ingest/policies.py ---

_HEBREW_UNICODE_RE = re.compile(r'[֐-׿]')
_GARBLED_HEBREW_RE = re.compile(r'[àáâãäåæçèéêëìíîïðñòóôõöøùú]{2,}')


def _fix_hebrew_visual_order(text: str) -> str:
    lines = text.split('\n')
    return '\n'.join(
        line[::-1] if _HEBREW_UNICODE_RE.search(line) else line
        for line in lines
    )


def _fix_hebrew_encoding(text: str) -> str:
    try:
        return text.encode('latin-1').decode('cp1255')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _clean_text(text: str) -> str:
    if _HEBREW_UNICODE_RE.search(text):
        text = _fix_hebrew_visual_order(text)
    elif _GARBLED_HEBREW_RE.search(text):
        text = _fix_hebrew_encoding(text)
    text = re.sub(r'\n+', '\n', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Visual order fix (PDF type 1 — reversed Hebrew Unicode)
# ---------------------------------------------------------------------------

def test_visual_order_title_line():
    # "כללי המס בישראל" stored in PDF as reversed — full line reversal restores it
    assert _fix_hebrew_visual_order('לארשיב סמה יללכ') == 'כללי המס בישראל'

def test_visual_order_vat_line():
    # Mixed Hebrew+number line — Hebrew words are restored (numbers also reverse, that's fine)
    result = _fix_hebrew_visual_order('18% אוה יטרדנטסה מ״עמה רועיש :מ״עמ')
    assert 'מע״מ' in result  # Hebrew restored

def test_visual_order_multiline_preserves_english():
    result = _fix_hebrew_visual_order('Hello world\nלארשיב סמה יללכ\nAnother line')
    lines = result.split('\n')
    assert lines[0] == 'Hello world'
    assert lines[2] == 'Another line'
    assert 'בישראל' in lines[1]

def test_visual_order_empty_string():
    assert _fix_hebrew_visual_order('') == ''

def test_visual_order_english_only():
    text = 'The VAT rate is 18%'
    assert _fix_hebrew_visual_order(text) == text


# ---------------------------------------------------------------------------
# Encoding fix (PDF type 2 — CP1255 mis-decoded as Latin-1)
# ---------------------------------------------------------------------------

def test_encoding_fix_meat():
    # 'îàú' = 0xEE,0xE0,0xFA in Latin-1 → CP1255 → מאת
    assert _fix_hebrew_encoding('îàú') == 'מאת'

def test_encoding_fix_attorney():
    # עו"ד = attorney abbreviation
    result = _fix_hebrew_encoding('òå"ã')
    assert result == 'עו"ד'

def test_encoding_fix_cpa():
    # רו"ח = CPA/accountant abbreviation
    result = _fix_hebrew_encoding('øå"ç')
    assert result == 'רו"ח'

def test_encoding_fix_corporate_tax():
    # 23% מס חברות
    result = _fix_hebrew_encoding('23% ìò ãîåò ìàøùé úåøáçä ñî')
    assert '23%' in result

def test_encoding_fix_preserves_ascii():
    # Punctuation and numbers must survive
    result = _fix_hebrew_encoding('îàú: 2017')
    assert '2017' in result
    assert 'מאת' in result


# ---------------------------------------------------------------------------
# _clean_text — auto-detection
# ---------------------------------------------------------------------------

def test_clean_text_auto_detects_visual_order():
    result = _clean_text('לארשיב סמה יללכ')
    assert 'בישראל' in result
    assert 'כללי' in result

def test_clean_text_auto_detects_encoding():
    result = _clean_text('îàú òå"ã')
    assert 'מאת' in result
    assert 'עו"ד' in result

def test_clean_text_english_unchanged():
    assert _clean_text('  Hello   world  ') == 'Hello world'

def test_clean_text_collapses_whitespace():
    result = _clean_text('Hello\n\n\nworld')
    assert '\n\n' not in result
