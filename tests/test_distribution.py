"""Archive inspection used by unit tests and offline build validation."""
from pathlib import PurePosixPath
import tarfile
import zipfile

import pytest


def inspect_archive(path):
    if path.name.endswith('.whl'):
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
    else:
        with tarfile.open(path) as archive:
            files = {entry.name: archive.extractfile(entry).read() for entry in archive.getmembers() if entry.isfile()}
    forbidden = {'.hermes', '.git', '.venv', '__pycache__', '.pytest_cache', 'oracle.json', 'manifest.json', '.coverage'}
    for name, data in files.items():
        assert not forbidden.intersection(PurePosixPath(name).parts), name
        assert not name.endswith(('.pyc', '.pem', '.key')), name
        assert (b'/' + b'opt/data/') not in data and (b'/' + b'home/ubuntu/') not in data, name
        assert (b'-----BEGIN ' + b'PRIVATE KEY-----') not in data, name
    return len(files)


@pytest.mark.parametrize('name', ['.hermes/plans/private.md', 'oracle.json', 'src/__pycache__/x.pyc'])
def test_distribution_inspection_rejects_forbidden_entries(tmp_path, name):
    path = tmp_path / 'test.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(name, 'controlled test fixture')
    with pytest.raises(AssertionError):
        inspect_archive(path)
