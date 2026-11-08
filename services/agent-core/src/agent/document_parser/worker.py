"""Single-request synchronous entry point for source and frozen parser workers."""
from __future__ import annotations

from pathlib import Path
import sys

from agent.document_parser.protocol import (RESULT, RESULT_LIMIT, STATUS_LIMIT, decode_frame, encode_frame,
                                          managed_original, owned_stage, write_result)
from agent.storage import CoreDataError


def main() -> int:
    request_id = None
    try:
        # Read once before importing native dependencies. No concurrent stdin reader.
        line = sys.stdin.buffer.readline(STATUS_LIMIT + 1)
        if len(line) > STATUS_LIMIT or not line.endswith(b'\n'):
            raise ValueError('Invalid request frame')
        request = decode_frame(line)
        request_id = request['request_id']
        if not isinstance(request_id, str) or len(request_id) != 32:
            raise ValueError('Invalid request ID')
        data_dir = Path(request['data_dir'])
        original = managed_original(data_dir, request['input_path'])
        output = Path(request['output_path'])
        if (output.name != RESULT or output.parent.parent != data_dir / 'parser-staging'
                or not owned_stage(output.parent) or output.exists()):
            raise ValueError('Invalid staging path')
        limits = request['limits']
        result_limit = limits['result_bytes']
        if type(result_limit) is not int or not 0 < result_limit <= RESULT_LIMIT:
            raise ValueError('Invalid result limit')
        from agent.rag.documents import parse_document
        parsed = parse_document(original)
        write_result(output, request_id, parsed, result_limit)
        status = {'request_id': request_id, 'status': 'complete'}
        code = 0
    except CoreDataError as error:
        # Never send document content or native exception details over status/logs.
        status = {'request_id': request_id, 'status': 'error', 'code': error.code}
        code = 1
    except BaseException:
        status = {'request_id': request_id, 'status': 'error', 'code': 'rag_parser_failed'}
        code = 1
    sys.stdout.buffer.write(encode_frame(status))
    sys.stdout.buffer.flush()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
