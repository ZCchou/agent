"""Offline JSONL worker. Native OCR libraries stay outside the Core process."""
from __future__ import annotations

from contextlib import redirect_stdout
import json
import math
from pathlib import Path
import sys
import threading
import time

from agent.document_parser.protocol import managed_original
from agent.ocr.store import OcrModelStore


def scan_pdf(path, progress=None):
    import pypdfium2 as pdfium
    pages = []
    with pdfium.PdfDocument(str(path)) as document:
        if len(document) > 10_000:
            raise ValueError('PDF exceeds 10,000 pages')
        total = 0
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            total += len(text)
            if total > 20_000_000:
                raise ValueError('PDF text exceeds the limit')
            width, height = page.get_size()
            image_area = 0
            for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
                x0, y0, x1, y1 = obj.get_bounds()
                image_area += max(0, x1 - x0) * max(0, y1 - y0)
            ratio = min(1, image_area / (width * height))
            pages.append({'page': index + 1, 'text': text, 'width_points': width,
                'height_points': height, 'image_ratio': ratio,
                'needs_ocr': len(text.strip()) < 40 or ratio > .25 or (ratio > .01 and len(text.strip()) < 80)})
            page.close()
            if progress:
                progress({'completed': index + 1, 'total': len(document), 'page': index + 1})
    return {'pages': pages}


def render_pdf(path, number, dpi=200):
    import pypdfium2 as pdfium
    import numpy as np
    with pdfium.PdfDocument(str(path)) as document:
        if type(number) is not int or not 1 <= number <= len(document):
            raise ValueError('Invalid PDF page')
        page = document[number - 1]
        width, height = page.get_size()
        scale = min(dpi / 72, math.sqrt(20_000_000 / (width * height)))
        bitmap = page.render(scale=scale)
        image = np.array(bitmap.to_pil().convert('RGB'))[:, :, ::-1].copy()
        bitmap.close()
        page.close()
    return image


def main():
    engine = None
    profile_id = None
    engine_device = None
    output = sys.stdout
    sequence = 0
    for line in sys.stdin:
        request_id = None
        stopped = threading.Event()
        heartbeat = None
        lock = threading.Lock()
        version, identity = 1, {}

        def emit(frame):
            nonlocal sequence
            with lock:
                sequence += 1
                if version == 2:
                    frame = {**frame, 'protocol_version': 2, **identity, 'sequence': sequence}
                output.write(json.dumps(frame, ensure_ascii=True, allow_nan=False) + '\n')
                output.flush()

        def pulse():
            previous_cpu = time.process_time()
            while not stopped.wait(5):
                cpu = time.process_time()
                emit({'id': request_id, 'kind': 'heartbeat', 'cpu_seconds': cpu,
                      'resource_active': True if cpu - previous_cpu >= .02 else None})
                previous_cpu = cpu
        try:
            if len(line) > 1024 * 1024:
                raise ValueError('OCR request exceeds limit')
            request = json.loads(line)
            request_id = request['id']
            params = request['params']
            version = request.get('protocol_version', 1)
            identity = request.get('execution_identity', {})
            if version == 2:
                heartbeat = threading.Thread(target=pulse, daemon=True)
                heartbeat.start()
            data_dir = Path(params['data_dir']).absolute()
            original = managed_original(data_dir, params['path'])
            with redirect_stdout(sys.stderr):
                if request['action'] == 'scan':
                    result = scan_pdf(original, lambda status: emit({'id': request_id, 'progress': status})) if version == 2 else scan_pdf(original)
                elif request['action'] == 'recognize':
                    if engine is None or profile_id != params['profile_id'] or engine_device != params.get('device', 'auto'):
                        from agent.ocr.adapter import OcrAdapter
                        profile = OcrModelStore(data_dir / 'ocr-models').get_profile(params['profile_id'])
                        engine = OcrAdapter(profile, params.get('device', 'auto'), params.get('threads', 2))
                        profile_id = profile['id']
                        engine_device = params.get('device', 'auto')
                    result = engine.recognize_page(render_pdf(original, params['page']), params['page'])
                elif request['action'] == 'render':
                    import base64
                    import io
                    from PIL import Image
                    image = render_pdf(original, params['page'], 100)
                    stream = io.BytesIO()
                    Image.fromarray(image[:, :, ::-1]).save(stream, format='PNG')
                    result = {'data_url': 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()}
                else:
                    raise ValueError('Unsupported OCR operation')
            response = {'id': request_id, 'result': result}
        except Exception as error:
            print(type(error).__name__, str(error)[:400], file=sys.stderr, flush=True)
            response = {'id': request_id, 'error': f'{getattr(error, "code", "ocr_page_failed")}: {type(error).__name__}'}
        stopped.set()
        if heartbeat is not None:
            heartbeat.join()
        emit(response)


if __name__ == '__main__':
    main()
