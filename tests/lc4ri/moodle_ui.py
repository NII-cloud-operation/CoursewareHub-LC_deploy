"""Launch CoursewareHub from a real Moodle course, then execute and save a user Notebook."""
import base64
import sys
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect
from browser_evidence import BrowserRecording


def check_moodle(results, moodle, manager, identities, browser, *, expected_existing=False, repository=None):
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    records = []
    for role, username in identities.items():
        record = dict(role=role, status='running', passed=[])
        records.append(record)
        videos = results / 'videos' / role
        recording = BrowserRecording(browser, videos, results.parent.name + "-Moodle-" + role)
        context = recording.context
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.set_default_timeout(60000)
        requests = []
        page.on('request', lambda request: requests.append(dict(
            method=request.method, origin=urlparse(request.url).netloc,
            path=urlparse(request.url).path)))

        def passed(step):
            record['passed'].append(step)
            print('MOODLE LTI PASS:', role, step, flush=True)

        def fetch(path, method='GET', body=None):
            result = page.evaluate('''async ({path, method, body}) => {
                const cookie = document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                const token = (window.jhdata && window.jhdata.xsrf_token) ||
                    (cookie ? decodeURIComponent(cookie[1]) : '');
                const r = await fetch(path, {method, headers: {
                    'Content-Type': 'application/json', 'X-XSRFToken': token},
                    ...(body === null ? {} : {body: JSON.stringify(body)})});
                return {status: r.status, text: await r.text()};
            }''', dict(path=path, method=method, body=body))
            assert 200 <= result['status'] < 300, result
            return json.loads(result['text']) if result['text'] else None

        try:
            page.goto(moodle.url + '/login/index.php')
            page.locator('#username').fill(role)
            page.locator('#password').fill(moodle.password)
            page.locator('#loginbtn').click()
            page.wait_for_url(lambda url: '/login/index.php' not in str(url))
            page.goto(moodle.url + '/course/view.php?id=' + str(moodle.metadata['course_id']))
            expect(page.get_by_role('heading', name='CoursewareHub LTI E2E', exact=True)).to_be_visible()
            passed('logged in to Moodle and opened the enrolled course')
            with page.expect_response(lambda r:
                    r.url == 'https://coursewarehub.test/php/lti/service.php' and
                    r.request.method == 'POST', timeout=120000) as launch:
                page.get_by_role('link', name='Open CoursewareHub').first.click()
            response = launch.value
            assert response.status < 400, response.text()
            token = parse_qs(response.request.post_data)['id_token'][0]
            claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
            assert claims['iss'] == moodle.url, claims
            assert claims['aud'] == moodle.metadata['client_id'], claims
            claim = 'https://purl.imsglobal.org/spec/lti/claim/'
            assert claims[claim + 'deployment_id'] == moodle.metadata['deployment_id'], claims
            expected_role = '#Instructor' if role == 'teacher' else '#Learner'
            assert any(r.endswith(expected_role) for r in claims[claim + 'roles']), claims
            (results / (role + '-launch-claims.json')).write_text(json.dumps(claims, indent=2) + '\n')
            page.wait_for_url('https://coursewarehub.test/**', timeout=120000)
            page.goto('https://coursewarehub.test/hub/home')
            user = fetch('/hub/api/user')
            assert user['name'] == username and user['admin'] == (role == 'teacher'), user
            passed('Moodle LTI 1.3 launch authenticated the expected CoursewareHub user and permissions')
            from runtime_ui import spawn_user
            spawn_user(page,'https://coursewarehub.test',username)
            prefix = '/user/' + username + '/'
            if expected_existing:
                persisted = fetch(prefix + 'api/contents/moodle-persist.txt')
                assert persisted['content'] == role + '-lti', persisted
                saved = fetch(prefix + 'api/contents/moodle-runtime.ipynb')
                assert 'MOODLE_LTI_RESULT' in str(saved['content']), saved
                passed('Moodle launch after O01 retained the saved user data and Notebook')
            uid = int(manager.output(['id', '-u', username]))
            assert uid > 0
            source = ('import os, subprocess\nfrom pathlib import Path\n'
                      f'assert os.getuid() == {uid}, os.getuid()\n')
            if repository:
                from custom_image import marker_check
                source+=marker_check(repository)
            if role == 'student':
                source += 'assert subprocess.run(["sudo", "-n", "true"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).returncode != 0\n'
            source += f'Path("moodle-persist.txt").write_text({(role + "-lti")!r})\nprint("MOODLE_LTI_RESULT", {role!r}, 6 * 7)'
            notebook = dict(nbformat=4, nbformat_minor=5, metadata=dict(
                kernelspec=dict(name='python3', language='python', display_name='Python 3')),
                cells=[dict(cell_type='code', id='moodle-lti', metadata={},
                            execution_count=None, outputs=[], source=source)])
            fetch(prefix + 'api/contents/moodle-runtime.ipynb', 'PUT',
                  dict(type='notebook', format='json', content=notebook))
            page.goto('https://coursewarehub.test' + prefix + 'notebooks/moodle-runtime.ipynb')
            page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()')
            page.locator('.cell').first.locator('.input_prompt').click()
            page.keyboard.press('Escape')
            page.keyboard.press('Shift+Enter')
            expect(page.locator('.code_cell').first.locator('.output')).to_contain_text('MOODLE_LTI_RESULT ' + role + ' 42', timeout=120000)
            page.keyboard.press('Control+s')
            page.wait_for_function('!Jupyter.notebook.dirty')
            saved = fetch(prefix + 'api/contents/moodle-runtime.ipynb')
            assert 'MOODLE_LTI_RESULT ' + role + ' 42' in str(saved['content']['cells'][0]['outputs']), saved
            (results / (role + '-executed.ipynb')).write_text(json.dumps(saved['content'], indent=1) + '\n')
            page.screenshot(path=str(results / (role + '-executed.png')), full_page=True)
            passed('executed Python as the provisioned non-root UID and saved the Notebook')
            record['status'] = 'passed'
        finally:
            error = sys.exc_info()[1]
            if error is not None:
                record.update(status='failed', error=str(error))
                page.screenshot(path=str(results / (role + '-failure.png')), full_page=True)
                (results / (role + '-failure.html')).write_text(page.content())
            (results / 'moodle-results.json').write_text(json.dumps(records, indent=2) + '\n')
            (results / (role + '-requests.json')).write_text(json.dumps(requests, indent=2) + '\n')
            recording.finish(results / (role + '-trace.zip'))
