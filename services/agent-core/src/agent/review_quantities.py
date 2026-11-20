"""Conservative checks for explicit selected-quote quantity attributions.

This detects absent values, not semantic entailment, phase or bound direction.
"""
from decimal import Decimal
from fractions import Fraction
import re
import unicodedata

from agent.storage import CoreDataError

_NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,32})(?:\.\d{1,16})?'
_RAW_NUMBER = re.compile(r'(?<![A-Za-z0-9_.,])(' + _NUMBER + r')(?![A-Za-z0-9_.])')
_UNITS = {
    '小时': ('time', 3600), 'h': ('time', 3600),
    '分钟': ('time', 60), 'min': ('time', 60),
    '秒钟': ('time', 1), '秒': ('time', 1), 's': ('time', 1),
    '千帕': ('pressure', 1000), 'kPa': ('pressure', 1000),
    '帕': ('pressure', 1), 'Pa': ('pressure', 1),
    '摄氏度': ('celsius', 1), '°C': ('celsius', 1),
    'Hz': ('frequency', 1), 'kHz': ('frequency', 1000), 'MHz': ('frequency', 1000000),
    'V': ('voltage', 1), 'mV': ('voltage', Fraction(1, 1000)),
}
_QUANTITY = re.compile(r'(?<![A-Za-z0-9_.,])(' + _NUMBER + r')\s*('
    + '|'.join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True)) + r')(?![A-Za-z])')
_SCALAR_LITERAL = re.compile(r'\s*[<>=≤≥±]*\s*' + _NUMBER
    + r'(?:\s*[/~～至–-]\s*[<>=≤≥]*\s*' + _NUMBER + r')*\s*\.?\s*')
_EXTERNAL = re.compile(r'依据|根据|条款|标准|所[引选](?:摘录|证据|条款)|DO[- ]?160|'
    r'\b(?:standard|clause|according to|provided (?:quote|excerpt))\b', re.I)
_COMPARISON = re.compile(r'规定|要求|必须|符合|满足|一致|支持|\b(?:requires?|consistent|conforms?|supports?)\b', re.I)
_ORIGINAL = re.compile(r'原文|原格|原值|模板(?:采用|写|给出|规定|值)|\b(?:original|template)\b', re.I)
_UNCERTAIN = re.compile(r'未.{0,20}(?:规定|证明|证实|支持|验证|确认)|(?:无法|不能|尚不能).{0,12}(?:证明|确认|验证|认定)|'
    r'缺乏.{0,12}(?:依据|支持|证据)|尚无.{0,12}(?:依据|支持|证据)|待(?:核实|核对|验证|确认)|'
    r'(?:仍)?需(?:核实|核对|验证|确认)|\b(?:unverified|not supported|cannot confirm|not established)\b', re.I)
_DERIVED = re.compile(r'(?:计算|推算|估算|折算|换算)(?:得到|得出|为|成)|\b(?:calculated|derived|estimated)\b', re.I)


def _text(value):
    return unicodedata.normalize('NFKC', value).translate(str.maketrans('−－﹣', '---'))


def _value(value):
    return Fraction(Decimal(value.replace(',', '')))


def quantities(text):
    result = []
    for match in _QUANTITY.finditer(_text(text)):
        dimension, factor = _UNITS[match[2]]
        result.append(((dimension, _value(match[1]) * factor), match.group(0)))
    return result


def _numbers(text):
    return {_value(match[1]) for match in _RAW_NUMBER.finditer(_text(text))}


def validate_quantitative_attributions(texts, metadata, original):
    """A known value must be present in the actual chosen quotes if attributed.

    Ambiguous symbols such as m, approximate calculations, comparators and test
    semantics are intentionally not certified by this presence check.
    """
    quotes = [source['quote'] for source in metadata if isinstance(source, dict)
              and isinstance(source.get('quote'), str)]
    supported = {value for quote in quotes for value, _ in quantities(quote)}
    selected_numbers = {value for quote in quotes for value in _numbers(quote)}
    normalized_original = _text(original)
    bare_values = _numbers(normalized_original) if _SCALAR_LITERAL.fullmatch(normalized_original) else set()
    missing = []
    for text in texts:
        # Decimal and clause-number dots are not sentence boundaries.
        for sentence in re.split(r'[。！？!?；;\n]|(?<!\d)\.(?!\d)', _text(text)):
            external_seen = False
            for clause in re.split(r'[，,](?!\d{3}(?:\D|$))', sentence):
                local_external = bool(_EXTERNAL.search(clause))
                external_seen |= local_external
                if not (external_seen or local_external) or _UNCERTAIN.search(clause) or _DERIVED.search(clause):
                    continue
                if _ORIGINAL.search(clause) and not _COMPARISON.search(clause):
                    continue
                candidates = list(quantities(clause))
                for value, label in candidates:
                    if value not in supported and label not in missing:
                        missing.append(label)
                if bare_values and (_COMPARISON.search(clause) or local_external):
                    for value in _numbers(clause) & bare_values:
                        if value not in selected_numbers:
                            label = str(float(value)) if value.denominator != 1 else str(value.numerator)
                            if label not in missing:
                                missing.append(label)
    if missing:
        raise CoreDataError('rag_citation_invalid',
            '所选摘录未包含被归属于依据的量值：' + '、'.join(missing[:6]) + '。',
            {'locator_issue': 'quantitative_value_not_in_quotes', 'missing_quantities': missing[:6]})
