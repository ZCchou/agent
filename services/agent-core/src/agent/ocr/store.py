"""Content-addressed, verified local OCR model packages."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading

from agent.rag.models import _files, _hash, _json, _safe_relative, _write_json
from agent.storage import CoreDataError


class OcrModelStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        configuration = self.root.parent / 'ocr-storage.json'
        if self.root.name == 'ocr-models' and configuration.is_file():
            try:
                value = _json(configuration)
                selected = Path(value['root'])
                if value.get('schema_version') != 1 or not selected.is_absolute() or str(selected).startswith('\\\\'):
                    raise ValueError('Invalid local OCR storage root')
                self.root = selected.resolve()
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise CoreDataError('ocr_model_root_invalid', 'OCR 模型存储配置无效，请在模型设置中重新选择本地目录。') from error
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def inspect(self, path):
        root = Path(path).absolute()
        if root.is_file():
            root = root.parent
        try:
            files = _files(root)
            manifest = _json(root / 'ocr-model.json')
            if manifest.get('schema_version') != 1 or manifest.get('profile') != 'pp-structure-v3':
                raise ValueError('Unsupported OCR model package profile')
            expected = manifest.get('files')
            if not isinstance(expected, dict) or not expected:
                raise ValueError('OCR model package has no files')
            actual = {item.relative_to(root).as_posix() for item in files
                      if item.name not in {'ocr-model.json', '.ocr-meta.json'}}
            if actual != set(expected):
                raise ValueError('OCR model file list differs from manifest')
            size = 0
            for name, digest in expected.items():
                file = _safe_relative(root, name)
                size += file.stat().st_size
                if size > 6 * 1024 ** 3:
                    raise ValueError('OCR model package exceeds 6 GiB')
                if not isinstance(digest, str) or len(digest) != 64 or _hash(file) != digest:
                    raise ValueError(f'OCR model hash mismatch: {name}')
            if not isinstance(manifest.get('models'), dict) or not manifest['models']:
                raise ValueError('OCR model roles are missing')
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                separators=(',', ':')).encode()).hexdigest()
            return {'valid': True, 'id': digest, 'digest': digest, 'name': manifest.get('name', root.name),
                    'profile': manifest['profile'], 'size_bytes': size,
                    'license': manifest.get('license', ''), 'manifest': manifest, 'path': str(root)}
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CoreDataError('ocr_model_invalid', str(error)) from error

    def import_bundle(self, path):
        with self._lock:
            inspected = self.inspect(path)
            destination = self.root / inspected['id']
            if not destination.exists():
                if shutil.disk_usage(self.root).free < inspected['size_bytes'] + 256 * 1024 ** 2:
                    raise CoreDataError('ocr_disk_full', 'Insufficient space for the OCR model package')
                with tempfile.TemporaryDirectory(prefix='ocr-import-', dir=self.root) as temporary:
                    stage = Path(temporary) / 'package'
                    shutil.copytree(inspected['path'], stage)
                    verified = self.inspect(stage)
                    if verified['digest'] != inspected['digest']:
                        raise CoreDataError('ocr_model_invalid', 'OCR model changed during import')
                    metadata = {key: value for key, value in verified.items() if key not in {'path', 'manifest'}}
                    _write_json(stage / '.ocr-meta.json', metadata)
                    stage.rename(destination)
            return self.get_profile(inspected['id'])

    def get_profile(self, profile_id):
        if not isinstance(profile_id, str) or len(profile_id) != 64 or any(c not in '0123456789abcdef' for c in profile_id):
            raise CoreDataError('ocr_model_invalid', 'Invalid OCR model identity')
        result = self.inspect(self.root / profile_id)
        if result['id'] != profile_id:
            raise CoreDataError('ocr_model_invalid', 'OCR model identity changed')
        return result

    def list_models(self):
        models = []
        for path in sorted(self.root.iterdir()):
            if len(path.name) == 64 and path.is_dir():
                try:
                    record = _json(path / '.ocr-meta.json')
                    models.append({**record, 'status': 'ready'})
                except (OSError, ValueError):
                    continue
        return {'models': models, 'root': str(self.root)}
