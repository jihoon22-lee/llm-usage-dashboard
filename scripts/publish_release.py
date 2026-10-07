"""Publish only the verified CI artifact; existing assets are immutable to this script."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from release_guard import compare_assets, sha256, verify_manifest


def api(path, method='GET', data=None, *, binary=False):
    url = path if path.startswith('https://') else 'https://api.github.com' + path
    # Never send the job token to an arbitrary host from downloaded release metadata.
    if urllib.parse.urlsplit(url).hostname not in {'api.github.com','uploads.github.com'}:
        raise ValueError('Untrusted GitHub API endpoint')
    headers = {'Authorization':'Bearer ' + os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json',
               'X-GitHub-Api-Version':'2022-11-28'}
    if binary:
        headers['Content-Type'] = 'application/octet-stream'
    elif data is not None:
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method)) as response:
        return json.load(response)


def require_remote_tag(repo, tag, commit):
    reference = api('/repos/' + repo + '/git/ref/tags/' + urllib.parse.quote(tag, safe=''))
    target = reference['object']
    for _ in range(16):
        if target['type'] == 'commit':
            if target['sha'] != commit:
                raise ValueError('Remote release tag moved away from the tested commit')
            return
        if target['type'] != 'tag':
            raise ValueError('Release tag does not resolve to a commit')
        import re
        if not re.fullmatch('[0-9a-f]{40}', target['sha']):
            raise ValueError('Invalid annotated tag identifier')
        target = api('/repos/' + repo + '/git/tags/' + target['sha'])['object']
    raise ValueError('Annotated tag nesting exceeds the release safety limit')


def main():
    folder = Path(sys.argv[1])
    tag = os.environ['RELEASE_TAG']
    repo = os.environ['GITHUB_REPOSITORY']
    manifest = verify_manifest(folder, commit=os.environ['GITHUB_SHA'], tag=tag, run_id=os.environ['GITHUB_RUN_ID'])
    if manifest['repository'] != repo:
        raise ValueError('Repository provenance mismatch')
    require_remote_tag(repo, tag, manifest['commit'])
    prefix = '/repos/' + repo + '/releases'
    try:
        release = api(prefix + '/tags/' + urllib.parse.quote(tag, safe=''))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        release = None
    expected = {p.name:sha256(p) for p in folder.iterdir()}
    existing = {}
    if release:
        if release['tag_name'] != tag:
            raise ValueError('Existing release tag identity does not match the requested tag')
        # Download through gh into a temporary folder; digest checks never trust asset metadata alone.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='release-verify-') as temporary:
            for asset in release['assets']:
                name = asset['name']
                if name not in expected or name in existing:
                    raise ValueError('Unexpected/duplicate release asset')
                target = Path(temporary) / name
                with target.open('wb') as output:
                    subprocess.run(['gh','api',f'/repos/{repo}/releases/assets/{asset["id"]}',
                                    '-H','Accept: application/octet-stream'], stdout=output, check=True)
                existing[name] = sha256(target)
    missing = compare_assets(expected, existing, published=bool(release and not release['draft']))
    if release and not release['draft']:
        if release.get('immutable') is not True:
            raise ValueError('Existing published release is not protected by GitHub immutable releases')
        print('Published release already matches every verified asset; no changes made')
        return
    if not release:
        release = api(prefix, 'POST', dict(tag_name=tag, target_commitish=manifest['commit'], name=tag, draft=True,
            body='Validated wheel and source archive. Verify SHA256SUMS before installation.\n\n'
                 f'Commit: `{manifest["commit"]}`\nCI run: https://github.com/{repo}/actions/runs/{manifest["run_id"]}\n'
                 f'Tested artifact ID: `{os.environ["TESTED_ARTIFACT_ID"]}`'))
    upload = release['upload_url'].split('{',1)[0]
    for name in missing:
        api(upload + '?name=' + urllib.parse.quote(name, safe=''), 'POST', (folder / name).read_bytes(), binary=True)
    # Re-download every draft asset before publication; no overwrite/delete API is used.
    fresh = api(prefix + '/' + str(release['id']))
    import tempfile
    with tempfile.TemporaryDirectory(prefix='release-final-') as temporary:
        actual = {}
        for asset in fresh['assets']:
            name = asset['name']
            if name not in expected or name in actual:
                raise ValueError('Unexpected/duplicate draft asset')
            target = Path(temporary) / name
            with target.open('wb') as output:
                subprocess.run(['gh','api',f'/repos/{repo}/releases/assets/{asset["id"]}',
                                '-H','Accept: application/octet-stream'],stdout=output,check=True)
            actual[name] = sha256(target)
        compare_assets(expected, actual, published=True)
    require_remote_tag(repo, tag, manifest['commit'])
    published = api(prefix + '/' + str(release['id']), 'PATCH', {'draft':False})
    if published.get('immutable') is not True:
        raise ValueError('GitHub did not confirm immutable release protection; administrator action is required')
    print('Published verified release ' + tag)


if __name__ == '__main__':
    main()
