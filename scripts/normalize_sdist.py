"""Canonicalize source-archive metadata before testing and hashing release bytes.

Setuptools honors SOURCE_DATE_EPOCH for wheels but currently records wall-clock
metadata in source archives. Contents are unchanged; tar/gzip timestamps and
builder identities are normalized so a workflow retry can verify existing assets.
"""
import gzip
import io
from pathlib import Path
import sys
import tarfile


def normalize(path, epoch):
    path = Path(path)
    with tarfile.open(path, 'r:gz') as archive:
        entries = []
        for member in archive.getmembers():
            if not (member.isfile() or member.isdir()):
                raise ValueError('Unexpected source archive member type')
            content = archive.extractfile(member).read() if member.isfile() else None
            member.uid = member.gid = 0
            member.uname = member.gname = ''
            member.mtime = int(epoch)
            member.pax_headers = {}
            entries.append((member, content))
    output = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=int(epoch), compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for member, content in sorted(entries,key=lambda entry:entry[0].name):
                archive.addfile(member,io.BytesIO(content) if content is not None else None)
    path.write_bytes(output.getvalue())


if __name__ == '__main__':
    source, = Path(sys.argv[1]).glob('*.tar.gz')
    normalize(source,int(sys.argv[2]))
