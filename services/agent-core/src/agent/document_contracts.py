"""Bounded application-owned document tools."""
from agent.storage import CoreDataError

DOCUMENT_TOOLS = ('document.inspect_template', 'document.fill_template', 'document.export', 'document.read_attachment')

def definition(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}

DOCUMENT_DEFINITIONS = [
    definition(DOCUMENT_TOOLS[0], 'Inspect an authorized DOCX attachment. Reuse returned template_id and stable target IDs. Page through fields before filling.', {
        'attachment_id': {'type': 'string'}, 'template_id': {'type': 'string'}, 'path': {'type': 'string', 'maxLength': 1024},
        'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, []),
    definition(DOCUMENT_TOOLS[1], 'Fill explicit paragraph or table-cell targets in a verified template. Supply current evidence IDs and quote IDs for professional content. Missing information remains a draft.', {
        'template_id': {'type': 'string'},
        'fields': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'target_id': {'type': 'string'}, 'text': {'type': 'string', 'maxLength': 16000},
                'evidence': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object', 'properties': {
                    'id': {'type': 'string'}, 'quote_id': {'type': 'string'}}, 'required': ['id', 'quote_id'], 'additionalProperties': False}},
                'missing': {'type': 'array', 'maxItems': 32, 'items': {'type': 'string', 'maxLength': 1000}}},
            'required': ['target_id', 'text', 'evidence']}}}, ['template_id', 'fields']),
    definition(DOCUMENT_TOOLS[2], 'Publish a generated document through project file approval. Provide an artifact_id from template filling, or content and format. In evidence tasks content must be the validated claims JSON with current evidence IDs and quote IDs. Success is determined by the actual file result.', {
        'artifact_id': {'type': 'string'}, 'content': {'type': 'string', 'maxLength': 1000000},
        'format': {'type': 'string', 'enum': ['docx', 'markdown', 'txt']}, 'path': {'type': 'string', 'maxLength': 1024}}, ['path']),
    definition(DOCUMENT_TOOLS[3], 'Read an authorized document attachment as untrusted text with source locators. Continue using next_offset.', {
        'attachment_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        'physical_page': {'type': 'integer', 'minimum': 1}, 'section': {'type': 'string', 'maxLength': 1024}}, ['attachment_id']),
]


def validate_document_call(name, arguments):
    schema = next(item['function']['parameters'] for item in DOCUMENT_DEFINITIONS if item['function']['name'] == name)
    if set(arguments) - set(schema['properties']) or any(key not in arguments for key in schema['required']):
        raise CoreDataError('invalid_tool_call', '文档工具参数缺失或包含未知字段。')
    for key in {'attachment_id', 'template_id', 'artifact_id', 'path'} & arguments.keys():
        if not isinstance(arguments[key], str) or not 1 <= len(arguments[key]) <= 1024 or '\0' in arguments[key]:
            raise CoreDataError('invalid_tool_call', '文档标识或路径无效。')
    if name == DOCUMENT_TOOLS[3]:
        if type(arguments.get('offset', 0)) is not int or arguments.get('offset', 0) < 0 or type(arguments.get('limit', 20)) is not int or not 1 <= arguments.get('limit', 20) <= 100 or ('physical_page' in arguments and (type(arguments['physical_page']) is not int or arguments['physical_page'] < 1)) or ('section' in arguments and (not isinstance(arguments['section'], str) or not arguments['section'].strip() or len(arguments['section']) > 1024)):
            raise CoreDataError('invalid_tool_call', '文档读取范围无效。')
    elif name == DOCUMENT_TOOLS[0]:
        if sum(bool(arguments.get(key)) for key in ['attachment_id', 'template_id', 'path']) != 1:
            raise CoreDataError('invalid_tool_call', '请选择一个模板附件或模板标识。')
        if type(arguments.get('offset', 0)) is not int or arguments.get('offset', 0) < 0 \
                or type(arguments.get('limit', 100)) is not int or not 1 <= arguments.get('limit', 100) <= 100:
            raise CoreDataError('invalid_tool_call', '模板分页范围无效。')
    elif name == DOCUMENT_TOOLS[1]:
        fields = arguments['fields']
        if not isinstance(fields, list) or not 1 <= len(fields) <= 200 or any(not isinstance(field, dict) for field in fields):
            raise CoreDataError('invalid_tool_call', '模板填写项无效。')
        for field in fields:
            if set(field) - {'target_id', 'text', 'evidence', 'missing'} or not {'target_id', 'text', 'evidence'} <= set(field) \
                    or not isinstance(field['target_id'], str) or not 1 <= len(field['target_id']) <= 256 \
                    or not isinstance(field['text'], str) or len(field['text']) > 16000:
                raise CoreDataError('invalid_tool_call', '模板位置或正文无效。')
            citations, missing = field['evidence'], field.get('missing', [])
            if not isinstance(citations, list) or len(citations) > 8 or not isinstance(missing, list) or len(missing) > 32 \
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in missing):
                raise CoreDataError('invalid_tool_call', '模板证据或缺项清单无效。')
            if any(not isinstance(item, dict) or set(item) != {'id', 'quote_id'} or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in item.values()) for item in citations):
                raise CoreDataError('invalid_tool_call', '模板证据编号无效。')
    else:
        if bool(arguments.get('artifact_id')) == ('content' in arguments):
            raise CoreDataError('invalid_tool_call', '请选择文档产物或提供正文。')
        if 'content' in arguments and (not isinstance(arguments['content'], str) or not arguments['content'].strip()
                or len(arguments['content']) > 1000000 or arguments.get('format') not in {'docx', 'markdown', 'txt'}):
            raise CoreDataError('invalid_tool_call', '导出内容或格式无效。')
