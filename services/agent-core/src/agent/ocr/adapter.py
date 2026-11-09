"""Fixed PP-StructureV3 configuration with explicit local model directories."""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import socket

from agent.ocr.contracts import normalize_page, restore_bbox
from agent.storage import CoreDataError

ROLES = {
    'layout_detection', 'doc_orientation_classify', 'text_detection', 'textline_orientation',
    'text_recognition', 'table_classification', 'wired_table_structure_recognition',
    'wireless_table_structure_recognition', 'wired_table_cells_detection',
    'wireless_table_cells_detection', 'table_orientation_classify',
}


def offline_network(*args, **kwargs):
    raise PermissionError('OCR inference is offline')


class OcrAdapter:
    def __init__(self, profile, device='auto', threads=2):
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
        os.environ['HF_HUB_OFFLINE'] = '1'
        socket.socket.connect = offline_network
        socket.create_connection = offline_network
        manifest = profile['manifest']
        models = manifest['models']
        if set(models) != ROLES:
            raise CoreDataError('ocr_model_invalid', 'OCR model bundle must contain all supported model roles')
        for package, version in manifest.get('runtime', {}).items():
            if importlib.metadata.version(package) != version:
                raise CoreDataError('ocr_runtime_incompatible', f'OCR requires {package} {version}')
        root = Path(profile['path']).resolve()
        kwargs = {}
        for role, model in models.items():
            directory = (root / model['directory']).resolve()
            if not directory.is_relative_to(root) or not directory.is_dir():
                raise CoreDataError('ocr_model_invalid', 'OCR model directory is outside its package')
            kwargs[role + '_model_name'] = model['name']
            kwargs[role + '_model_dir'] = str(directory)
        import paddle
        has_cuda = paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
        if device == 'cuda' and not has_cuda:
            raise CoreDataError('ocr_device_unavailable', 'OCR CUDA backend is unavailable')
        self.device = 'gpu:0' if has_cuda and device in {'auto', 'cuda'} else 'cpu'
        from paddleocr import PPStructureV3
        self.pipeline = PPStructureV3(**kwargs, device=self.device, cpu_threads=threads,
            enable_mkldnn=False, use_doc_orientation_classify=True, use_doc_unwarping=False,
            use_textline_orientation=True, use_table_recognition=True, use_formula_recognition=False,
            use_seal_recognition=False, use_chart_recognition=False, use_region_detection=False)

    def recognize_page(self, image, page):
        # PaddleX 3.7.2 disables the table subpipeline's OCR model when page OCR
        # is supplied. Cell re-recognition can dereference that absent model.
        result = next(iter(self.pipeline.predict(image, use_table_orientation_classify=False,
            use_ocr_results_with_table_cells=False)))
        raw = result.json['res']
        # The pipeline's page-space dimensions accompany all result coordinates.
        normalized = normalize_page(raw, page, raw['width'], raw['height'])
        normalized['device'] = self.device
        normalized['orientation'] = raw.get('doc_preprocessor_res', {}).get('angle', 0)
        for region in normalized['regions']:
            region['bbox'] = restore_bbox(region['bbox'], normalized['orientation'])
        normalized['width'], normalized['height'] = image.shape[1], image.shape[0]
        return normalized
