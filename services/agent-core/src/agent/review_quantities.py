"""Conservative checks for explicit selected-quote quantity attributions.

This detects absent values, not semantic entailment, phase or bound direction.
"""
from dataclasses import dataclass
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
    '%': ('percent', 1),
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


_LOWER = {'≥', '>=', '至少', '最小', '不小于', '不低于', 'at least', 'minimum'}
_UPPER = {'≤', '<=', '最多', '最多允许', '最大', '不大于', '不高于', '不超过', '至多', 'within', 'at most', 'maximum'}
_STRICT_LOWER = {'>', '大于', '高于', 'more than'}
_STRICT_UPPER = {'<', '小于', '低于', 'less than'}
_UNIT_EXPRESSION = '|'.join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True))
_BOUND_PREFIX = (
    r'(?:(?P<operator>' + '|'.join(map(re.escape, sorted(_LOWER | _UPPER | _STRICT_LOWER | _STRICT_UPPER,
                                                         key=len, reverse=True))) + r')\s*)?'
    r'(?P<value>' + _NUMBER + r')\s*(?:(?P<separator>~|至|到|–|—|-|±|to)\s*(?P<second>' + _NUMBER + r')\s*)?')
_BOUND_SUFFIX = r'\s*(?P<suffix>以内|内|以上|以下)?'
_BOUND = re.compile(_BOUND_PREFIX + r'(?P<unit>' + _UNIT_EXPRESSION
                    + r')(?:\s*/\s*(?P<per>' + _UNIT_EXPRESSION + r'))?' + _BOUND_SUFFIX)
# Two explicitly identified operands may use the same count unit even if it
# has no conversion factor. Never guess a new unit from unstructured prose.
_COUNT_BOUND = re.compile(_BOUND_PREFIX + r'(?P<unit>[a-zA-Z]{1,16}|[\u3400-\u9fff]{1,8}?)' + _BOUND_SUFFIX)


@dataclass(frozen=True)
class _Interval:
    dimension: tuple
    low: Fraction | None
    high: Fraction | None
    low_inclusive: bool = True
    high_inclusive: bool = True

    def within(self, other):
        if self.dimension != other.dimension:
            return None
        if other.low is not None and (self.low is None or self.low < other.low
                or self.low == other.low and self.low_inclusive and not other.low_inclusive):
            return False
        if other.high is not None and (self.high is None or self.high > other.high
                or self.high == other.high and self.high_inclusive and not other.high_inclusive):
            return False
        return True


def _constraint(text):
    text = _text(text).strip()
    match = _BOUND.fullmatch(text) or _COUNT_BOUND.fullmatch(text)
    if not match:
        return None
    dimension, factor = _UNITS.get(match['unit'], ('literal:' + match['unit'], 1))
    denominator = match.groupdict().get('per')
    per, divisor = _UNITS[denominator] if denominator else (None, 1)
    dimension = dimension, per
    factor = Fraction(factor, divisor)
    value = _value(match['value']) * factor
    operator, suffix = match['operator'], match['suffix']
    if operator and suffix:
        return None
    if match['second']:
        if operator or suffix:
            return None
        second = _value(match['second']) * factor
        low, high = (value-second, value+second) if match['separator'] == '±' else (value, second)
        return _Interval(dimension, low, high) if low <= high else None
    if operator in _LOWER | _STRICT_LOWER or suffix == '以上':
        return _Interval(dimension, value, None, low_inclusive=operator not in _STRICT_LOWER)
    if operator in _UPPER | _STRICT_UPPER or suffix in ('以内', '内', '以下'):
        return _Interval(dimension, None, value, high_inclusive=operator not in _STRICT_UPPER)
    return _Interval(dimension, value, value)


def quantitative_subset(actual, required):
    """Compare a pair explicitly identified as the same metric and phase. Unknown returns None."""
    actual_interval, required_interval = _constraint(actual), _constraint(required)
    if actual_interval and required_interval:
        return actual_interval.within(required_interval)
    return None


def single_quantitative_constraint(text):
    """Extract an operand only when the passage contains exactly one supported constraint."""
    text = _text(text)
    matches = list(_BOUND.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    return match[0].strip() if _complete_match(text, match) else None


def _complete_match(text, match):
    if re.search(r'(?:不|非|未|约|近|不得|大约|\bnot|\bapprox(?:imately)?|[\d.,±<>≤≥])\s*$', text[:match.start()], re.I):
        return False
    return not re.match(r'[A-Za-z0-9_/±~]', text[match.start()+len(match[0].rstrip()):])


def grounded_operand(label, excerpt):
    """Recover the actual bound that contains a displayed amount; do not drop its inequality."""
    label = _text(label).strip()
    annotated = re.fullmatch(r'(.+?)\s*\(([^()]+)\)', label)
    if annotated:
        label, conversion = annotated.groups()
        if not _constraint(label) or _constraint(label) != _constraint(conversion.strip()):
            return None
    selected = _constraint(label)
    if selected is None:
        return None
    excerpt = _text(excerpt)
    candidates = []
    for match in _BOUND.finditer(excerpt):
        # Ignore a partial unit or an unsupported approximation/negation.
        if not _complete_match(excerpt, match):
            continue
        candidate = _constraint(match[0])
        if (candidate and selected.dimension == candidate.dimension and
            (selected == candidate or selected.low is not None and selected.low == selected.high
             and (candidate.low is None or candidate.high is None) and selected.low in (candidate.low, candidate.high))):
            candidates.append(match[0].strip())
    if candidates:
        return candidates[0] if len(candidates) == 1 else None
    if (selected.dimension[0].startswith('literal:') and
        re.search(r'(?<![0-9.,±+−-])' + re.escape(label) + r'(?![0-9A-Za-z])', excerpt)):
        return label
    return None


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
