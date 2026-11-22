import io
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from agent.document_inventory import inventory

def sym(paragraph,font='Wingdings',code='F0E0'):
 run=paragraph.add_run();node=OxmlElement('w:sym');node.set(qn('w:font'),font);node.set(qn('w:char'),code);run._r.append(node)

def test_word_symbol_in_body_table_and_header_keeps_boundary_and_target_identity():
 doc=Document();p=doc.add_paragraph('75.26kPa');sym(p);p.add_run('11.60kPa')
 cell=doc.add_table(rows=1,cols=1).cell(0,0);cell.text='A';sym(cell.paragraphs[0]);cell.paragraphs[0].add_run('B')
 h=doc.sections[0].header.paragraphs[0];h.add_run('Start');sym(h);h.add_run('End')
 payload=io.BytesIO();doc.save(payload);values=inventory(payload.getvalue())['fields'];by={v['id']:v for v in values}
 assert by['p-0']['text']=='75.26kPa→11.60kPa'
 assert by['t-0-r0-c0']['text']=='A→B'
 assert any(v['text']=='Start→End' and v['kind']=='story' for v in values)
 assert by['p-0']['original_content']==by['p-0']['text']

def test_unknown_symbol_font_and_code_are_explicit_not_silently_joined():
 doc=Document();p=doc.add_paragraph('left');sym(p,'UnknownCustom','F099');p.add_run('right')
 b=io.BytesIO();doc.save(b);text=inventory(b.getvalue())['fields'][0]['text']
 assert text!='leftright' and 'UnknownCustom' in text and 'F099' in text
