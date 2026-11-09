"""Build-machine helper that prepares an entirely local PP-StructureV3 package."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

MODELS = {
    'layout_detection': 'PP-DocLayout-S',
    'doc_orientation_classify': 'PP-LCNet_x1_0_doc_ori',
    'text_detection': 'PP-OCRv5_mobile_det',
    'textline_orientation': 'PP-LCNet_x1_0_textline_ori',
    'text_recognition': 'PP-OCRv5_mobile_rec',
    'table_classification': 'PP-LCNet_x1_0_table_cls',
    'wired_table_structure_recognition': 'SLANeXt_wired',
    'wireless_table_structure_recognition': 'SLANet_plus',
    'wired_table_cells_detection': 'RT-DETR-L_wired_table_cell_det',
    'wireless_table_cells_detection': 'RT-DETR-L_wireless_table_cell_det',
    'table_orientation_classify': 'PP-LCNet_x1_0_doc_ori',
}


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def prepare(destination):
    destination.mkdir(parents=True, exist_ok=True)
    archives = destination.parent / 'ocr-downloads'
    archives.mkdir(exist_ok=True)
    sources = []
    roles = {}
    for role, name in MODELS.items():
        target = destination / 'models' / name
        archive = archives / (name + '_infer.tar')
        url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/' + archive.name
        if not target.exists():
            if not archive.exists():
                print('Downloading', name, flush=True)
                request = urllib.request.Request(url, headers={'User-Agent': 'agent-ocr-build/1'})
                with urllib.request.urlopen(request, timeout=120) as response, archive.with_suffix('.part').open('wb') as output:
                    shutil.copyfileobj(response, output)
                archive.with_suffix('.part').replace(archive)
            staging = archives / (name + '-extracted')
            staging.mkdir(exist_ok=True)
            with tarfile.open(archive) as content:
                content.extractall(staging, filter='data')
            candidates = list(staging.rglob('inference.pdiparams'))
            if len(candidates) != 1:
                raise ValueError(f'Expected one inference model in {name}')
            target.parent.mkdir(exist_ok=True)
            shutil.copytree(candidates[0].parent, target)
        roles[role] = {'name': name, 'directory': 'models/' + name}
        sources.append({'model': name, 'url': url, 'archive_sha256': digest(archive)})
    files = {file.relative_to(destination).as_posix(): digest(file)
             for file in sorted(destination.rglob('*')) if file.is_file() and file.name != 'ocr-model.json'}
    manifest = {'schema_version': 1, 'profile': 'pp-structure-v3', 'name': 'PP-StructureV3 local document OCR',
        'license': 'Apache-2.0', 'license_source': 'https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE',
        'runtime': {'paddleocr': '3.7.0', 'paddlex': '3.7.2', 'paddlepaddle': '3.3.1'},
        'models': roles, 'files': files, 'sources': sources}
    (destination / 'ocr-model.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Prepared', destination, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    prepare(parser.parse_args().destination)
