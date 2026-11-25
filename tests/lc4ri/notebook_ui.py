"""Build and exercise CoursewareHub or OperationHub through the LC4RI Notebook UI."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote
import urllib.request
from urllib.error import URLError

from browser_evidence import BrowserRecording
from notebook_controls import source
from lab_controls import BASE, operations, open_working_copy
from scenario import run_local_coursewarehub, run_federated_coursewarehub, run_operationhub

from playwright.sync_api import sync_playwright


def resolve_revision(variable):
    selection = os.environ.get(variable)
    if not selection:
        return None
    repository, _, ref = selection.partition('@')
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository) or any(part in ('.', '..') for part in repository.split('/')) or not ref:
        raise ValueError(f'{variable}: expected owner/repository@branch, @refs/pull/N/head or @commit')
    url = f'https://github.com/{repository}.git'
    if re.fullmatch('[0-9a-f]{40}', ref):
        return url, ref
    full_ref = ref if ref.startswith('refs/') else f'refs/heads/{ref}'
    if not (full_ref.startswith('refs/heads/') or re.fullmatch(r'refs/pull/[1-9][0-9]*/(head|merge)', full_ref)):
        raise ValueError(f'{variable}: expected a branch, pull request ref or commit')
    subprocess.run(['git', 'check-ref-format', full_ref], check=True)
    output = subprocess.check_output(['git', 'ls-remote', '--refs', '--exit-code', url, full_ref],
                                     text=True, timeout=60)
    sha, resolved_ref = output.strip().split('\t')
    if resolved_ref != full_ref or not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError(f'{variable}: could not resolve {selection}')
    return url, sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--master', required=True)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--operationhub', action='store_true')
    parser.add_argument('--authentication', choices=('local','federation'), default='local')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    results = args.result_dir.resolve()
    results.mkdir(parents=True, exist_ok=True)
    token = os.environ['LC4RI_TEST_TOKEN']
    records, steps = [], []
    args.fixtures = ExitStack()
    args.build_repository = None
    args.admin_password = None
    args.group = 'ci_lc4ri'
    args.workers = [args.worker]
    args.test_moodle = not args.operationhub and args.authentication == 'local'
    args.saml = None
    if not args.operationhub:
        args.jupyterhub_deploy = resolve_revision('LC4RI_JUPYTERHUB_DEPLOY')
        args.manage_tools = resolve_revision('LC4RI_MANAGE_TOOLS')
    if not args.operationhub and args.authentication == 'federation':
        from saml_provider import running_provider
        args.saml = args.fixtures.enter_context(running_provider('http://' + os.environ['LC4RI_GATEWAY'] + ':18501', 'https://coursewarehub.test', 'ciadmin@example.test'))
    if args.operationhub:
        from operationhub import setup
        setup(args, results)

    def api(path, method='GET', body=None):
        request = urllib.request.Request(BASE + '/api/contents/' + quote(path, safe='/'), method=method,
            headers={'Authorization': 'token ' + token, 'Content-Type': 'application/json'},
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    for attempt in range(90):
        try:
            api('')
            break
        except (ConnectionResetError, URLError) as error:
            if isinstance(error, URLError) and not isinstance(error.reason, (ConnectionRefusedError, ConnectionResetError)):
                raise
            if attempt == 89:
                raise TimeoutError("LC4RI did not accept connections") from error
            time.sleep(1)

    if args.saml is not None:
        metadata=args.saml.metadata().decode()
        api('ci-idp-metadata.xml','PUT',dict(type='file',format='text',content=metadata))
        (results/'identity-provider-metadata.xml').write_text(metadata)

    inventory = f'[ci_lc4ri]\n{args.master} public_ip={args.master}\n{args.worker}\n\n[ci_lc4ri:vars]\nansible_user=centos\nansible_ssh_private_key_file=~/.ssh/ansible_id_rsa\n'
    if not args.operationhub:
        args.hub_image = os.environ['LC4RI_HUB_IMAGE']
        api('hosts', 'PUT', dict(type='file', format='text', content=inventory))

    def passed(name):
        steps.append(name)
        print('LC4RI PASS:', name, flush=True)
        summary('running')

    def summary(status, error=None):
        (results / 'results.json').write_text(json.dumps(dict(status=status, passed=steps,
            operations=records, error=error), ensure_ascii=False, indent=2) + '\n')

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        size = dict(width=1600, height=1000)
        recording = BrowserRecording(browser, results / 'construction-videos', '', viewport=size)
        context = recording.context
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = None
        key = None
        hashes = {}

        def run_notebook(filename, procedure, *, evidence_name=None):
            nonlocal page, key
            path = repo / filename
            key = evidence_name or path.stem.split('_', 1)[0]
            data = path.read_bytes()
            original = json.loads(data)
            hashes[key] = hashlib.sha256(data).hexdigest()
            (results / 'source-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
            api(key + '-template.ipynb', 'PUT', dict(type='notebook', format='json', content=original))
            recording.chapter = path.stem
            page = context.new_page()
            page.set_default_timeout(60000)
            document = key + '-working.ipynb'
            open_working_copy(page, BASE, token, key + '-template.ipynb', document)
            texts = page.evaluate('window.jupyterapp.shell.currentWidget.context.model.toJSON().cells.map(c => c.source)')
            assert texts == [source(c) for c in original['cells']], 'Working copy differs from the original'
            passed(key + ': saved a working copy with original cell sources')
            controls = operations(page, key, original, records, timeout=900000)
            procedure(original, args, controls, page, records, results)
            page.keyboard.press('Control+s')
            page.wait_for_function('!window.jupyterapp.shell.currentWidget.context.model.dirty')
            saved = api(document)['content']
            count = len(original['cells'])
            assert len(saved['cells']) in (count, count + 1), 'Unexpected cells added to working copy'
            if len(saved['cells']) > count:
                extra = saved['cells'][-1]
                assert extra['cell_type'] == 'code' and source(extra) == '' and not extra.get('outputs')
                records.append(dict(notebook=key, action='Shift+Enter appended an empty cell at the end'))
            for index, cell in enumerate(saved['cells'][:count]):
                assert source(cell) == controls.edits.get(index, source(original['cells'][index])), (key, index)
            (results / (key + '-working.ipynb')).write_text(json.dumps(saved, ensure_ascii=False, indent=1) + '\n')
            page.screenshot(path=str(results / (key + '-completed.png')), full_page=True)
            page.close()
            page = None
            assert path.read_bytes() == data, 'Tracked Notebook original changed'
            passed(key + ': completed original procedure through LC4RI UI')

        try:
            if args.operationhub:
                run_operationhub(run_notebook, args, results, browser)
            elif args.authentication == 'federation':
                run_federated_coursewarehub(run_notebook, args, results, browser, api)
            else:
                run_local_coursewarehub(run_notebook, args, results, browser, api)
            summary('passed')
        finally:
            error = sys.exc_info()[1]
            if error is not None:
                summary('failed', str(error))
                if page is not None and not page.is_closed():
                    page.screenshot(path=str(results / 'failure.png'), full_page=True)
                    (results / 'failure.html').write_text(page.content())
                    saved = page.evaluate('() => window.jupyterapp?.shell.currentWidget?.context?.model?.toJSON?.()')
                    if saved is not None:
                        (results / (str(key) + '-failure.ipynb')).write_text(json.dumps(saved, ensure_ascii=False, indent=1))
            recording.finish(results / 'browser-trace.zip')
            browser.close()
            args.fixtures.close()


if __name__ == '__main__':
    main()
