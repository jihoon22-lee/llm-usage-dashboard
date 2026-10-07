import gzip
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('normalize_sdist',Path(__file__).resolve().parents[1]/'scripts/normalize_sdist.py')
normalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalizer)


class SourceArchiveTests(unittest.TestCase):
    def test_canonical_bytes_preserve_contents_across_build_times_and_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary)/'first.tar.gz',Path(temporary)/'second.tar.gz']
            for index,path in enumerate(paths):
                with gzip.GzipFile(filename=str(path),mode='wb',mtime=100+index) as compressed:
                    with tarfile.open(fileobj=compressed,mode='w') as archive:
                        entry = tarfile.TarInfo('package/module.py')
                        content = b'print("synthetic")\n'
                        entry.size=len(content);entry.mtime=200+index;entry.uid=index;entry.uname='builder'+str(index)
                        archive.addfile(entry,io.BytesIO(content))
                normalizer.normalize(path,50)
            self.assertEqual(paths[0].read_bytes(),paths[1].read_bytes())
            with tarfile.open(paths[0]) as archive:
                self.assertEqual(archive.extractfile('package/module.py').read(),content)
                self.assertEqual(archive.getmember('package/module.py').mtime,50)
            before=paths[0].read_bytes();normalizer.normalize(paths[0],50)
            self.assertEqual(paths[0].read_bytes(),before)
