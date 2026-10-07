"""Shared validation for privileged installer entry points.

Fail before service changes rather than interpolate unsupported values into systemd
or sudoers. None of these helpers reads provider credentials or usage records.
"""
import os
from pathlib import Path
import pwd
import re
import stat


def service_path(value):
    text=str(value)
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',text) or '..' in Path(text).parts:
        raise ValueError('설치 경로는 공백·특수 문자 없는 절대 경로여야 합니다.')
    return Path(text)


def resolve_owner(value,project):
    name=value or os.environ.get('SUDO_USER')
    if not name or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',name):
        raise ValueError('--owner 또는 일반 사용자 SUDO_USER가 필요합니다.')
    try:owner=pwd.getpwnam(name)
    except KeyError:raise ValueError('설치 소유자 계정이 존재하지 않습니다.') from None
    if owner.pw_uid==0:raise ValueError('root를 설치 소유자로 사용할 수 없습니다.')
    service_path(owner.pw_dir);service_path(project)
    if not Path(owner.pw_dir).is_dir():raise ValueError('설치 소유자의 홈 디렉터리가 존재하지 않습니다.')
    return owner


def prepare_data_directory(path,owner):
    """Create owner-writable private directories; never traverse symlinks as root."""
    path=service_path(path)
    missing=[]
    for directory in (path,*path.parents):
        try:info=directory.lstat()
        except FileNotFoundError:missing.append(directory);continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('데이터 경로는 심볼릭 링크 없는 디렉터리여야 합니다.')
    if path.exists() and path.stat().st_uid!=owner.pw_uid:
        raise ValueError('기존 데이터 디렉터리의 소유자가 설치 소유자와 다릅니다.')
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chown(directory,owner.pw_uid,owner.pw_gid)
    path.chmod(0o700)
