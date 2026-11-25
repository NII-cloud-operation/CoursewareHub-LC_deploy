"""Verify the deployed Hub by logging in, running a user kernel, and restarting it."""
from notebook_controls import source

from contextlib import contextmanager
import sys
import json
from pathlib import Path
import time
from urllib.parse import quote

from playwright.sync_api import expect

from construction import check_git_log, remote
from browser_evidence import BrowserRecording



def spawn_user(page, base, username, image=None):
    response=page.goto(base+'/hub/spawn',wait_until='domcontentloaded')
    assert response is not None and response.status<400,page.locator('body').inner_text()
    choices=page.locator('input[name="image"]')
    if choices.count():
        if image is not None:
            page.locator('input[name="image"][value="'+image+'"]').check()
        else:
            assert page.locator('input[name="image"]:checked').count()==1
        page.locator('input[type="submit"], button[type="submit"]').click()
    page.wait_for_url('**/user/'+username+'/**',timeout=600000)
    page.wait_for_function('window.Jupyter && Jupyter.notebook_list',timeout=120000)


def stop_user(page, base, username, fetch, hosts):
    page.goto(base+'/hub/home')
    page.locator('#stop').click()
    for attempt in range(90):
        user=fetch('/hub/api/user')
        if not user.get('server') and '' not in user.get('servers',{}):break
        time.sleep(2)
    else:raise AssertionError('User server did not stop')
    for attempt in range(90):
        if all(not remote(host,"docker ps -q --filter label=com.docker.swarm.service.name=jupyter-"+username) for host in hosts):break
        time.sleep(2)
    else:raise AssertionError('Old user container did not exit')


class AdminNotebookSession:
    base = 'https://coursewarehub.test'

    def __init__(self, args, results, page):
        self.args = args
        self.results = results
        self.page = page
        self.steps = []

    def save(self, status, error=None):
        (self.results / 'runtime.json').write_text(json.dumps(
            dict(status=status, passed=self.steps, error=error), indent=2))

    def passed(self, text):
        self.steps.append(text)
        print('RUNTIME PASS:', text, flush=True)
        self.save('running')

    def fetch(self, path, method='GET', body=None):
        value = self.page.evaluate('''async ({path,method,body}) => {
            const c=document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
            const token=(window.jhdata && jhdata.xsrf_token) || (c ? decodeURIComponent(c[1]):'');
            const r=await fetch(path,{method,signal:AbortSignal.timeout(30000),headers:{'Content-Type':'application/json','X-XSRFToken':token},
                body:body===null?undefined:JSON.stringify(body)});
            return {status:r.status,text:await r.text()};}''',dict(path=path,method=method,body=body))
        assert 200 <= value['status'] < 300, value
        return json.loads(value['text']) if value['text'] else None

    def log_in(self):
        if self.args.saml is not None:
            from saml_provider import launch_saml
            self.args.saml.mode='valid'
            launch_saml(self.page,self.args.saml)
        else:
            self.page.goto(self.base + '/php/login.php')
            self.page.locator('input[name="email"]').fill('ciadmin@example.test')
            self.page.locator('input[name="password"]').fill(self.args.admin_password)
            self.page.locator('input[type="submit"], button[type="submit"]').click()
        self.page.wait_for_function("location.pathname.startsWith('/hub/') || location.pathname.startsWith('/user/')", timeout=120000)
        self.page.goto(self.base + '/hub/home')
        user=self.fetch('/hub/api/user')
        assert user['admin'], user
        self.username=user['name']
        self.args.teacher_username=self.username
        self.prefix='/user/'+quote(self.username,safe='')+'/'
        self.passed('administrator logged into the constructed Hub')

    def build_default_image(self):
        from custom_image import build_environment
        from guest import Guest
        build_environment(self.page,self.base,self.args.build_repository,self.results,Guest('master',self.args.master,'ssh'))
        self.passed('Git source was built through the environment-management UI and selected as the default image')
        self.page.goto(self.base+'/hub/home')
        current=self.fetch('/hub/api/user')
        if current.get('server') or '' in current.get('servers',{}):
            stop_user(self.page,self.base,self.username,self.fetch,(self.args.master,self.args.worker))

    def start_user_server(self):
        repository = self.args.build_repository
        spawn_user(self.page, self.base, self.username, repository['image'] if repository else None)
        self.passed('user server started and Notebook tree rendered')

    def verify_saved_file(self):
        assert self.fetch(self.prefix + 'api/contents/lc4ri-persist.txt')['content'] == 'persistent-user-data'
        self.passed('verified content saved by the administrator kernel')

    def run_kernel_and_save_file(self):
        repository = self.args.build_repository
        notebook={'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'name':'python3','language':'python','display_name':'Python 3'}},
            'cells':[{'cell_type':'code','id':'runtime-check','metadata':{},'execution_count':None,'outputs':[],'source':''}]}
        self.fetch(self.prefix+'api/contents/lc4ri-runtime.ipynb','PUT',{'type':'notebook','format':'json','content':notebook})
        self.page.goto(self.base+self.prefix+'notebooks/lc4ri-runtime.ipynb')
        self.page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        uid=int(remote(self.args.master, 'id -u '+self.username))
        assert uid > 0
        code=('import os\nfrom pathlib import Path\n'+f'assert os.getuid() == {uid}\n'+
            'Path("lc4ri-persist.txt").write_text("persistent-user-data")\nprint("LC4RI_RESULT", 6 * 7)')
        if repository:
            from custom_image import marker_check
            code=code.replace('print("LC4RI_RESULT", 6 * 7)',marker_check(repository)+'print("LC4RI_RESULT", 6 * 7)')
        self.page.locator('.cell').first.locator('.CodeMirror').click()
        self.page.keyboard.insert_text(code)
        self.page.keyboard.press('Shift+Enter')
        expect(self.page.locator('.cell').first.locator('.output')).to_contain_text('LC4RI_RESULT 42',timeout=120000)
        self.page.keyboard.press('Control+s')
        self.page.wait_for_function('!Jupyter.notebook.dirty')
        saved=self.fetch(self.prefix+'api/contents/lc4ri-runtime.ipynb')['content']
        (self.results/'runtime-working.ipynb').write_text(json.dumps(saved,ensure_ascii=False,indent=1))
        self.page.screenshot(path=str(self.results/'executed-cell.png'),full_page=True)
        self.passed('browser ran Python under the provisioned user UID and saved the Notebook')
        if repository:
            self.passed('the provisioned user kernel ran inside the repo2docker-built image')

    def trust_cluster_hosts(self):
        keys = [host+' '+remote(host,'cat /etc/ssh/ssh_host_ed25519_key.pub') for host in (self.args.master,self.args.worker)]
        import shlex
        home='/jupyter/users/'+self.username
        remote(self.args.master, "sh -c " + shlex.quote("printf '%s\\n' " + ' '.join(shlex.quote(key) for key in keys) + " > " + home + "/.ssh/known_hosts"))
        remote(self.args.master,'chown '+self.username+':'+self.username+' '+home+'/.ssh/known_hosts')

    def run_notebook(self, filename):
        self.page.goto(self.base+self.prefix+'notebooks/'+quote(filename))
        self.page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        original=self.page.evaluate('() => Jupyter.notebook.get_cells().map(c=>({type:c.cell_type,text:c.get_text()}))')
        canonical=json.loads((Path(__file__).resolve().parents[2]/'coursewarehub'/filename).read_text())
        assert original==[dict(type=c['cell_type'],text=source(c)) for c in canonical['cells']], 'Deployed D06 differs from the original source'
        self.page.locator('#menubar .dropdown-toggle').filter(has_text='File').click()
        with self.page.context.expect_page() as copying:
            self.page.locator('#copy_notebook').click()
        self.page=copying.value
        self.page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        assert self.page.evaluate('() => Jupyter.notebook.get_cells().map(c=>({type:c.cell_type,text:c.get_text()}))')==original
        from notebook_controls import operations
        d06_records=[]
        controls=operations(self.page,'D06',canonical,d06_records,timeout=300000)
        repository_cell = clone_cell = history_cell = None
        if self.args.manage_tools:
            url, sha = self.args.manage_tools
            setting = "REPOS_URL='https://github.com/NII-cloud-operation/CoursewareHub-LC_manage-tools.git'"
            clone = '!git clone {REPOS_URL} {work_dir}/{repo_name}'
            repository_cell = controls.find(setting)
            clone_cell = controls.find(clone)
            history_cell = controls.find('!cd {work_dir}/{repo_name} && git log -n 5')
        for index,cell in enumerate(original):
            if cell['type']!='code' or not cell['text'].strip():continue
            if index == repository_cell:
                updated = cell['text'].replace(setting, 'REPOS_URL=' + repr(url))
                if updated != cell['text']:
                    controls.edit(index, updated, 'Select the course management tools repository')
            if index == clone_cell:
                if cell['text'].splitlines().count(clone) != 1:
                    raise ValueError(f'Expected one complete clone command: {clone}')
                directory = '{work_dir}/{repo_name}'
                commands = (f'{clone} && git -C {directory} fetch origin {sha} '
                            f'&& git -C {directory} checkout --detach {sha}')
                controls.edit(index, cell['text'].replace(clone, commands), 'Select the course tools revision')
            observed=controls.run(index)
            if index == history_cell:
                check_git_log(observed, sha)
            if '-CD' not in cell['text']:
                assert not any('FAILED!' in str(o) or 'UNREACHABLE!' in str(o) for o in observed['outputs']),observed['outputs']
        expected = [dict(cell, text=controls.edits.get(i, cell['text'])) for i, cell in enumerate(original)]
        assert self.page.evaluate('()=>Jupyter.notebook.get_cells().slice(0,'+str(len(original))+').map(c=>({type:c.cell_type,text:c.get_text()}))')==expected
        (self.results/'D06-operations.json').write_text(json.dumps(d06_records,indent=2)+'\n')
        self.page.keyboard.press('Control+s')
        self.page.wait_for_function('!Jupyter.notebook.dirty')
        (self.results/'D06-working.ipynb').write_text(json.dumps(self.page.evaluate('()=>Jupyter.notebook.toJSON()'),ensure_ascii=False,indent=1))

    def verify_deployed_notebook(self, filename):
        assert self.fetch(self.prefix + 'api/contents/' + quote(filename))['type'] == 'notebook'
        self.passed('verified deployed Notebook: ' + filename)

    def stop_user_server(self):
        stop_user(self.page, self.base, self.username, self.fetch, (self.args.master, self.args.worker))


@contextmanager
def admin_notebook_session(args, results, browser):
    results.mkdir(parents=True, exist_ok=True)
    videos = results / 'videos'
    recording = BrowserRecording(browser, videos, results.name + "-CoursewareHub-kernel-and-D06", viewport={'width':1600,'height':1000})
    context = recording.context
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    page.set_default_timeout(60000)
    session = AdminNotebookSession(args, results, page)
    try:
        yield session
        session.save('passed')
    finally:
        error = sys.exc_info()[1]
        if error is not None:
            session.save('failed', str(error))
            session.page.screenshot(path=str(results/'runtime-failure.png'), full_page=True)
            (results/'runtime-failure.html').write_text(session.page.content())
            notebook = session.page.evaluate('()=>window.Jupyter?.notebook?.toJSON()')
            if notebook is not None:
                (results/'runtime-failure.ipynb').write_text(json.dumps(notebook, ensure_ascii=False, indent=1))
        recording.finish(results/'browser-trace.zip')
