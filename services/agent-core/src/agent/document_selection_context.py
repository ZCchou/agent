"""Bounded DOCX source observations for internal selection, never authority.

Direct run formatting is reported without claiming full Word style inheritance.
Candidate example associations never decide inclusion or exclusion.
"""
import re

from agent.document_plan import heading_level
from agent.documents import targets

ANCHOR = re.compile(r'^\s*(?:示例|举例|example)\s*[:：]', re.I)


def selection_annotations(document, fields, source_targets=None):
    mapped = source_targets if source_targets is not None else {
        identity: (target, kind) for identity, target, kind in targets(document)}
    output = {}
    section_anchor = None
    italic_anchor = None
    for field in fields:
        identity = field['id']
        if identity not in mapped or not field.get('text', '').strip():
            continue
        target, kind = mapped[identity]
        paragraphs = [target] if kind == 'paragraph' else target.paragraphs
        level = heading_level(target) if kind == 'paragraph' else None
        context = {'version': 1, 'authority': 'source_observation_only',
                   'heading_level': level,
                   'paragraph_styles': list(dict.fromkeys(p.style.name for p in paragraphs))[:4],
                   'explicit_italic_chars': sum(len(r.text) for p in paragraphs for r in p.runs if r.italic is True),
                   'explicit_bold_chars': sum(len(r.text) for p in paragraphs for r in p.runs if r.bold is True),
                   'run_text_chars': sum(len(r.text) for p in paragraphs for r in p.runs)}
        # Paragraph offsets only: cells join multiple paragraphs differently, so
        # never publish offsets that would appear to address their flat text.
        if kind == 'paragraph':
            offset, spans = 0, []
            for run in target.runs:
                if run.text and (run.italic is True or run.bold is True):
                    spans.append({'start': offset, 'end': offset + len(run.text),
                                  'italic': run.italic is True, 'bold': run.bold is True})
                offset += len(run.text)
            if offset == len(text := field.get('text', '')) and spans:
                context['explicit_run_spans'] = spans[:8]
                context['run_spans_truncated'] = len(spans) > 8
        path = field.get('section_ids', [])
        if section_anchor and (not path or section_anchor['section_id'] not in path):
            section_anchor = None
        if level or kind != 'paragraph':
            italic_anchor = None
        text = field.get('text', '')
        match = ANCHOR.match(text) if kind == 'paragraph' and not level else None
        association = None
        if match:
            association = {'anchor_id': identity, 'relation': 'explicit_prefix',
                           'anchor_only': not text[match.end():].strip()}
            if association['anchor_only']:
                section_anchor = {'id': identity, 'section_id': path[-1] if path else None}
                italic_anchor = None
            else:
                # A prefix containing sample prose is not a section-wide switch.
                section_anchor = None
                italic_anchor = identity if context['explicit_italic_chars'] > 0 else None
        elif (italic_anchor and kind == 'paragraph' and text.strip()
              and context['explicit_italic_chars'] == context['run_text_chars'] > 0):
            association = {'anchor_id': italic_anchor, 'relation': 'candidate_italic_continuation'}
        else:
            if kind == 'paragraph' and text.strip() and not level:
                italic_anchor = None
            if section_anchor and not level:
                association = {'anchor_id': section_anchor['id'], 'relation': 'candidate_section_context'}
        if association:
            context['example_context'] = association
        useful_style = any(re.search(r'示例|example|instruction|指导|填写|quote|emphasis', name, re.I)
                           for name in context['paragraph_styles'])
        if level or association or useful_style or context['explicit_italic_chars'] or context['explicit_bold_chars']:
            if not level:
                context.pop('heading_level')
            for key in ('explicit_italic_chars', 'explicit_bold_chars'):
                if not context[key]:
                    context.pop(key)
            styles = [name for name in context['paragraph_styles'] if name not in {'Normal', 'Default Paragraph Font'}]
            if styles:
                context['paragraph_styles'] = styles
            else:
                context.pop('paragraph_styles')
            output[identity] = context
    return output
