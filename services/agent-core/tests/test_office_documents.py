import zipfile

import pytest

from agent.storage import CoreDataError

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PPT = 'http://schemas.openxmlformats.org/presentationml/2006/main'


def package(tmp_path, suffix, entries):
    path = tmp_path / ('sample' + suffix)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def spreadsheet():
    return {
        'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Safety" sheetId="7" r:id="r2"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="r2" Target="worksheets/sheet7.xml"/></Relationships>',
        'xl/sharedStrings.xml': f'<sst xmlns="{S}"><si><r><t>Shared </t></r><r><t>text</t></r></si></sst>',
        'xl/worksheets/sheet7.xml': f'<worksheet xmlns="{S}"><sheetData><row r="3"><c r="B3" t="s"><v>0</v></c><c r="D3" t="inlineStr"><is><t>Inline</t></is></c><c r="F3"><f>1+1</f><v>2</v></c><c r="G3"><f>WEBSERVICE("http://example.test")</f></c></row></sheetData></worksheet>',
    }


@pytest.mark.parametrize('suffix', ['.xlsx', '.xlsm', '.xltx', '.xltm', '.et', '.ett'])
def test_spreadsheet_values_positions_and_cached_formulas(tmp_path, suffix):
    from agent.document_parser.office import parse_office_document
    result = parse_office_document(package(tmp_path, suffix, spreadsheet()))
    assert [c['text'] for c in result['chunks']] == ['Shared text', 'Inline', '2']
    assert [(c['locator']['sheet'], c['locator']['cell']) for c in result['chunks']] == [('Safety', 'B3'), ('Safety', 'D3'), ('Safety', 'F3')]
    assert any('cached' in w.lower() for w in result['warnings'])


@pytest.mark.parametrize('suffix', ['.pptx', '.pptm', '.potx', '.potm', '.ppsx', '.ppsm', '.dps', '.dpt'])
def test_presentation_order_and_table_positions(tmp_path, suffix):
    from agent.document_parser.office import parse_office_document
    entries = {
        'ppt/presentation.xml': f'<p:presentation xmlns:p="{PPT}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="b"/><p:sldId id="257" r:id="a"/></p:sldIdLst></p:presentation>',
        'ppt/_rels/presentation.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="a" Target="slides/slide1.xml"/><Relationship Id="b" Target="slides/slide2.xml"/></Relationships>',
        'ppt/slides/slide1.xml': f'<p:sld xmlns:p="{PPT}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Second</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
        'ppt/slides/slide2.xml': f'<p:sld xmlns:p="{PPT}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>First</a:t></a:r></a:p></p:txBody></p:sp><p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Cell</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>',
    }
    result = parse_office_document(package(tmp_path, suffix, entries))
    assert [c['text'] for c in result['chunks']] == ['First', 'Cell', 'Second']
    assert [c['locator']['slide'] for c in result['chunks']] == [1, 1, 2]
    assert result['chunks'][1]['locator']['table'] == 1
    assert result['chunks'][1]['locator']['column'] == 1


@pytest.mark.parametrize('suffix', ['.docx', '.docm', '.dotx', '.dotm', '.wps', '.wpt'])
def test_word_container_detection_ignores_suffix(tmp_path, suffix):
    from agent.document_parser.office import parse_office_document
    word = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Word text</w:t></w:r></w:p></w:body></w:document>'
    result = parse_office_document(package(tmp_path, suffix, {'word/document.xml': word}))
    assert result['chunks'][0]['text'] == 'Word text'


@pytest.mark.parametrize('suffix', ['.doc', '.dot', '.xls', '.xlt', '.xlsb', '.ppt', '.pot', '.pps', '.wps', '.wpt', '.et', '.ett', '.dps', '.dpt', '.rtf'])
def test_legacy_files_report_missing_managed_converter(tmp_path, monkeypatch, suffix):
    from agent.document_parser import conversion
    from agent.document_parser.office import parse_office_document
    monkeypatch.setattr(conversion, 'converter_executable', lambda: tmp_path / 'missing' / 'soffice.exe')
    path = tmp_path / ('legacy' + suffix)
    path.write_bytes(b'{\\rtf1 hello}' if suffix == '.rtf' else b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1binary')
    with pytest.raises(CoreDataError) as error:
        parse_office_document(path)
    assert error.value.code == 'rag_office_converter_unavailable'


@pytest.mark.parametrize('data', [b'PK\x03\x04broken', b'not an office file'])
def test_malformed_modern_documents_rejected(tmp_path, data):
    from agent.document_parser.office import parse_office_document
    path = tmp_path / 'broken.xlsx'
    path.write_bytes(data)
    with pytest.raises(CoreDataError) as error:
        parse_office_document(path)
    assert error.value.code == 'rag_invalid_document'


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_xml_entities_rejected(tmp_path, encoding):
    from agent.document_parser.office import parse_office_document
    entries = spreadsheet()
    entries['xl/worksheets/sheet7.xml'] = '<!DOCTYPE worksheet [<!ENTITY x "unsafe">]><worksheet/>'.encode(encoding)
    with pytest.raises(CoreDataError) as error:
        parse_office_document(package(tmp_path, '.xlsx', entries))
    assert error.value.code == 'rag_invalid_document'


def test_expansion_limit_and_external_relationship_rejected(tmp_path):
    from agent.document_parser.office import parse_office_document
    with pytest.raises(CoreDataError) as error:
        parse_office_document(package(tmp_path, '.xlsx', spreadsheet()), max_expanded_bytes=50)
    assert error.value.code == 'rag_document_too_large'
    entries = spreadsheet()
    entries['xl/_rels/workbook.xml.rels'] = f'<Relationships xmlns="{P}"><Relationship Id="r2" Target="https://example.test/data" TargetMode="External"/></Relationships>'
    with pytest.raises(CoreDataError) as error:
        parse_office_document(package(tmp_path, '.xlsx', entries))
    assert error.value.code == 'rag_invalid_document'


def test_encrypted_ooxml_container_reports_password_status(tmp_path):
    from agent.document_parser.office import parse_office_document
    path = tmp_path / 'encrypted.docx'
    path.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'EncryptedPackage')
    with pytest.raises(CoreDataError) as error:
        parse_office_document(path)
    assert error.value.code == 'rag_document_encrypted'


def test_duplicate_zip_members_rejected(tmp_path):
    from agent.document_parser.office import parse_office_document
    path = package(tmp_path, '.xlsx', spreadsheet())
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('xl/workbook.xml', '<workbook/>')
    with pytest.raises(CoreDataError) as error:
        parse_office_document(path)
    assert error.value.code == 'rag_invalid_document'


def test_spreadsheet_media_warns_that_images_require_ocr(tmp_path):
    from agent.document_parser.office import parse_office_document
    entries = spreadsheet()
    entries['xl/media/image1.png'] = b'image fixture'
    result = parse_office_document(package(tmp_path, '.xlsx', entries))
    assert any('images' in warning.lower() and 'ocr' in warning.lower() for warning in result['warnings'])


def test_conversion_temporary_output_removed_after_use(tmp_path, monkeypatch):
    import sys
    from agent.document_parser import conversion
    from agent.infrastructure.processes import launch_contained
    script = tmp_path / 'converter.py'
    script.write_text('''import pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--outdir') + 1])
(out / 'source.docx').write_bytes(b'converted fixture')
''', encoding='utf-8')
    monkeypatch.setattr(conversion, 'converter_executable', lambda: script)
    monkeypatch.setattr(conversion, 'launch_contained', lambda command, **kwargs:
                        launch_contained([sys.executable, str(script), *command[1:]], **kwargs))
    source = tmp_path / 'original.doc'
    source.write_bytes(b'original immutable content')
    with conversion.convert_office(source, '.docx') as converted:
        assert converted.read_bytes() == b'converted fixture'
        stage = converted.parent.parent
    assert not stage.exists()
    assert source.read_bytes() == b'original immutable content'


def test_conversion_timeout_reaps_process_and_removes_stage(tmp_path, monkeypatch):
    import sys
    from agent.document_parser import conversion
    from agent.infrastructure.processes import launch_contained
    script = tmp_path / 'converter.py'
    script.write_text('import time; time.sleep(60)', encoding='utf-8')
    launched = []
    def launch(command, **kwargs):
        process, job = launch_contained([sys.executable, str(script)], **kwargs)
        launched.append((process, kwargs['cwd']))
        return process, job
    monkeypatch.setattr(conversion, 'converter_executable', lambda: script)
    monkeypatch.setattr(conversion, 'launch_contained', launch)
    monkeypatch.setattr(conversion, 'CONVERSION_TIMEOUT', .1)
    source = tmp_path / 'original.doc'
    source.write_bytes(b'original')
    with pytest.raises(CoreDataError) as error:
        with conversion.convert_office(source, '.docx'):
            pytest.fail('Timed-out conversion produced output')
    assert error.value.code == 'rag_office_conversion_timeout'
    assert launched[0][0].poll() is not None
    assert not launched[0][1].exists()


def test_converter_profile_disables_active_content_at_supported_registry_path(tmp_path):
    import xml.etree.ElementTree as ET
    from agent.document_parser.conversion import _profile
    _profile(tmp_path / 'profile')
    root = ET.parse(tmp_path / 'profile/user/registrymodifications.xcu').getroot()
    oor = '{http://openoffice.org/2001/registry}'
    settings = {(item.get(oor + 'path'), prop.get(oor + 'name')): prop.findtext('value')
                for item in root for prop in item}
    scripting = '/org.openoffice.Office.Common/Security/Scripting'
    assert settings.get((scripting, 'DisableActiveContent')) == 'true'
    assert settings.get((scripting, 'DisableMacrosExecution')) == 'true'
    assert settings.get((scripting, 'BlockUntrustedRefererLinks')) == 'true'
    assert settings.get((scripting, 'DisableOLEAutomation')) == 'true'
    assert settings.get(('/org.openoffice.Office.Writer/Content/Update', 'Link')) == '0'
    assert settings.get(('/org.openoffice.Office.Calc/Content/Update', 'Link')) == '1'


def test_conversion_cancellation_reaps_live_child_and_removes_stage(tmp_path, monkeypatch):
    import sys
    import threading
    from agent.document_parser import conversion
    from agent.infrastructure.processes import launch_contained
    script = tmp_path / 'converter.py'
    script.write_text('import time; time.sleep(60)', encoding='utf-8')
    cancel = threading.Event()
    launched = []
    def launch(command, **kwargs):
        process, job = launch_contained([sys.executable, str(script)], **kwargs)
        launched.append((process, kwargs['cwd']))
        cancel.set()
        return process, job
    monkeypatch.setattr(conversion, 'converter_executable', lambda: script)
    monkeypatch.setattr(conversion, 'launch_contained', launch)
    source = tmp_path / 'source.doc'
    source.write_bytes(b'original')
    with pytest.raises(CoreDataError) as error:
        with conversion.convert_office(source, '.docx', cancel=cancel):
            pytest.fail('Cancelled conversion produced output')
    assert error.value.code == 'rag_office_conversion_cancelled'
    assert launched[0][0].poll() is not None
    assert not launched[0][1].exists()


def test_conversion_stage_retries_locked_cleanup_and_preserves_body_error(monkeypatch):
    import tempfile
    from pathlib import Path
    from agent.document_parser import conversion
    cleanup = tempfile.TemporaryDirectory.cleanup
    attempts = []
    def briefly_locked(directory):
        attempts.append(directory.name)
        if len(attempts) == 1:
            raise PermissionError(13, 'Conversion working directory is briefly locked')
        cleanup(directory)
    monkeypatch.setattr(tempfile.TemporaryDirectory, 'cleanup', briefly_locked)
    failure = ValueError('Original conversion error')
    with pytest.raises(ValueError) as error:
        with conversion._conversion_stage() as temporary:
            stage = Path(temporary)
            (stage / 'partial').write_text('partial', encoding='utf-8')
            raise failure
    assert error.value is failure
    assert len(attempts) == 2
    assert not stage.exists()
