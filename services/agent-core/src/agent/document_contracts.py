"""Bounded application-owned document tools."""
from agent.storage import CoreDataError

DOCUMENT_TOOLS = ('document.inspect_template', 'document.fill_template', 'document.export', 'document.read_attachment', 'document.review_targets')

def document_batch_schema(task, attachment_ids):
    """Constrain model choices to writable positions in this authorized batch."""
    def object_schema(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}

    strings = {'type': 'array', 'items': {'type': 'string'}}
    parameters = {'type': 'array', 'maxItems': 100, 'items': object_schema({
        'name': {'type': 'string'}, 'value': {'type': 'string'}})}
    writable = [field.get('target_id', field.get('id')) for field in task['fields']
                if field.get('field_type') not in ({'container'} if task['stage'] == 'review' else {'heading', 'container'})]
    target_id = {'type': 'string', **({'enum': writable} if writable else {})}
    citation = object_schema({'id': {'type': 'string'}, 'quote_id': {'type': 'string'}})
    if task['stage'] == 'plan':
        target = object_schema({'target_id': target_id,
            'field_type': {'type': 'string', 'enum': ['instruction', 'example', 'placeholder', 'content']},
            'parameter_keys': strings,
            'span': {'anyOf': [{'type': 'null'}, object_schema({
                'start': {'type': 'integer', 'minimum': 0}, 'end': {'type': 'integer', 'minimum': 1}})]}})
        completed = object_schema({'targets': {'type': 'array', 'maxItems': len(writable), 'items': target},
                                   'equipment_parameters': parameters, 'gaps': strings})
    elif task['stage'] == 'review':
        sources = task.get('evidence', [])
        citation_options = []
        for source in sources:
            quote_ids = list(dict.fromkeys(quote['quote_id'] for quote in source.get('quote_options', [])))
            if quote_ids:
                citation_options.append(object_schema({
                    'id': {'type': 'string', 'enum': [source['id']]},
                    'quote_id': {'type': 'string', 'enum': quote_ids}}))
        if citation_options:
            citation = citation_options[0] if len(citation_options) == 1 else {'anyOf': citation_options}
        review_strings = {'type': 'array', 'maxItems': 200,
                          'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}
        reviewed = object_schema({'target_id': target_id,
            'content_kind': {'type': 'string', 'enum': ['instruction', 'example', 'placeholder', 'content', 'filled_content', 'heading']},
            'status': {'type': 'string', 'enum': ['reviewed', 'pending']},
            'summary': {'type': 'string', 'minLength': 1, 'maxLength': 8000}, 'problems': review_strings,
            'evidence': {'type': 'array', 'maxItems': 8 if citation_options else 0, 'items': citation},
            'version_issues': review_strings, 'gaps': review_strings})
        if task.get('include_facts'):
            reviewed['properties']['facts'] = {'type': 'array', 'maxItems': 20,
                'items': object_schema({key: {'type': 'string', 'maxLength': 256,
                    **({'minLength': 1} if key in {'name', 'value'} else {})}
                    for key in ('name', 'value', 'unit', 'condition')})}
            reviewed['required'].append('facts')
        completed = object_schema({'results': {'type': 'array', 'minItems': len(writable),
                                               'maxItems': len(writable), 'items': reviewed}})
    else:
        field = object_schema({'target_id': target_id, 'text': {'type': 'string', 'maxLength': 16000},
            'evidence': {'type': 'array', 'maxItems': 8, 'items': citation}, 'missing': strings,
            'parameter_values': parameters})
        completed = object_schema({'fields': {'type': 'array', 'minItems': len(writable),
                                              'maxItems': len(writable), 'items': field}})
    alternatives = [completed]
    if attachment_ids:
        properties = DOCUMENT_DEFINITIONS[3]['function']['parameters']['properties']
        reading = {name: {'anyOf': [schema, {'type': 'null'}]} for name, schema in properties.items()
                   if name not in {'attachment_id', 'cursor'}}
        reading = {'attachment_id': {'type': 'string', 'enum': list(attachment_ids)}, **reading}
        alternatives.append(object_schema({'read_requests': {'type': 'array', 'minItems': 1,
            'maxItems': 8, 'items': object_schema(reading)}}))
    return object_schema({'response': {'anyOf': alternatives}})


def definition(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}

DOCUMENT_DEFINITIONS = [
    definition(DOCUMENT_TOOLS[0], 'Inspect an authorized DOCX attachment. Supply exactly one of attachment_id, template_id, or path and omit the other two. Reuse returned template_id and stable target IDs. Page through fields before filling.', {
        'attachment_id': {'type': 'string', 'description': 'ID of the uploaded attachment from the current request. Use this for the first inspection of an uploaded file.'},
        'template_id': {'type': 'string', 'description': 'Template ID returned by a previous inspection. Omit it before the first inspection.'},
        'path': {'type': 'string', 'maxLength': 1024, 'description': 'An existing project-relative DOCX file path, never an attachment ID or template ID. Omit this for uploaded attachments.'},
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
    definition(DOCUMENT_TOOLS[3], 'Read an authorized document attachment as untrusted text with source locators. Continue using next_cursor or next_offset.', {
        'attachment_id': {'type': 'string'}, 'cursor': {'type': 'string', 'maxLength': 512}, 'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        'physical_page': {'type': 'integer', 'minimum': 1}, 'section': {'type': 'string', 'maxLength': 1024}}, ['attachment_id']),
    definition(DOCUMENT_TOOLS[4], 'Review a registered DOCX template using project evidence and persistent coverage. Omit target_ids to review every nonempty position. Returns a managed report artifact. Continue with revision or export when the user requested them.', {
        'template_id': {'type': 'string'},
        'target_ids': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                       'items': {'type': 'string', 'maxLength': 256}},
        'format': {'type': 'string', 'enum': ['markdown', 'docx', 'txt']}}, ['template_id']),
]

# The model-visible schema must express the same source choice as the executor.
DOCUMENT_DEFINITIONS[0]['function']['parameters']['oneOf'] = [
    {'required': [name]} for name in ('attachment_id', 'template_id', 'path')
]


def validate_document_call(name, arguments):
    schema = next(item['function']['parameters'] for item in DOCUMENT_DEFINITIONS if item['function']['name'] == name)
    if set(arguments) - set(schema['properties']) or any(key not in arguments for key in schema['required']):
        raise CoreDataError('invalid_tool_call', '文档工具参数缺失或包含未知字段。')
    for key in {'attachment_id', 'template_id', 'artifact_id', 'path'} & arguments.keys():
        if not isinstance(arguments[key], str) or not 1 <= len(arguments[key]) <= 1024 or '\0' in arguments[key]:
            raise CoreDataError('invalid_tool_call', '文档标识或路径无效。')
    if name == DOCUMENT_TOOLS[4]:
        ids = arguments.get('target_ids')
        if ids is not None and (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in ids)
                or len(set(ids)) != len(ids)):
            raise CoreDataError('invalid_tool_call', '审阅位置清单无效。')
        if arguments.get('format', 'markdown') not in {'markdown', 'docx', 'txt'}:
            raise CoreDataError('invalid_tool_call', '审阅报告格式无效。')
    elif name == DOCUMENT_TOOLS[3]:
        if 'cursor' in arguments and (not isinstance(arguments['cursor'], str) or not 1 <= len(arguments['cursor']) <= 512):
            raise CoreDataError('invalid_tool_call', '文档读取游标无效。')
        if type(arguments.get('offset', 0)) is not int or arguments.get('offset', 0) < 0 or type(arguments.get('limit', 20)) is not int or not 1 <= arguments.get('limit', 20) <= 100 or ('physical_page' in arguments and (type(arguments['physical_page']) is not int or arguments['physical_page'] < 1)) or ('section' in arguments and (not isinstance(arguments['section'], str) or not arguments['section'].strip() or len(arguments['section']) > 1024)):
            raise CoreDataError('invalid_tool_call', '文档读取范围无效。')
    elif name == DOCUMENT_TOOLS[0]:
        if sum(bool(arguments.get(key)) for key in ['attachment_id', 'template_id', 'path']) != 1:
            raise CoreDataError('invalid_tool_call', '请选择一个模板附件、模板标识或项目路径，并省略另外两个参数。',
                                {'tool_argument_constraint': 'template_identity'})
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
