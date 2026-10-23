"""Real bundled-model filesystem acceptance; no model parsing or shared-store writes.

Run on the disposable API with: python - < scripts/verify/model_integrity.py
Existing pulled embedding-model bytes are copied into a temporary package/store,
then all temporary content is removed. The live store is only read.
"""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch
from config import settings
from services import model_bundle as models

model = settings.embed_model
assert models.is_installed(model), 'Review embedding model is not byte-consistent'
print('PASS existing review embedding model has matching referenced bytes', flush=True)
files = models.export_model(model, Path())
original = {source: source.stat().st_mtime_ns for _, source in files}
with tempfile.TemporaryDirectory(prefix='model-integrity-', dir=settings.upload_dir) as directory:
    root = Path(directory); pkg = root / 'package'; store = root / 'store'
    for relative, source in files:
        destination = pkg / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    total = sum(source.stat().st_size for _, source in files)
    with patch.object(settings, 'ollama_models_dir', str(store)):
        models.install_model(pkg, model)
        assert models.is_installed(model)
        print(f'PASS real bundled install verified {len(files)-1} referenced files, {total} bytes', flush=True)
        mp = models.manifest_path(model)
        manifest_bytes = mp.read_bytes()
        digests = models._digests(json.loads(manifest_bytes))
        stamps = {models.blob_path(d): models.blob_path(d).stat().st_mtime_ns for d in digests}
        models.install_model(pkg, model)
        assert {p: p.stat().st_mtime_ns for p in stamps} == stamps
        print('PASS already-present real blobs reused unchanged', flush=True)
        name, tag = models.split_ref(model)
        damaged = pkg / 'models' / name / tag / 'blobs' / digests[-1].replace(':', '-')
        damaged.write_bytes(b'ordinary mismatched review bytes')
        try:
            models.install_model(pkg, model)
        except ValueError:
            pass
        else:
            raise AssertionError('Mismatched package blob was accepted')
        assert mp.read_bytes() == manifest_bytes
        assert models.is_installed(model)
        assert {p: p.stat().st_mtime_ns for p in stamps} == stamps
        print('PASS mismatched bundle refused; existing manifest and healthy blobs unchanged', flush=True)
    assert {p: p.stat().st_mtime_ns for p in original} == original
    print('PASS original shared model store left unchanged', flush=True)
print('PASS disposable model package and target removed', flush=True)
