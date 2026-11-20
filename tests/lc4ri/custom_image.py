"""Build a real course image through the deployed environment-management UI."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
import time


def run(argv):
    return subprocess.check_output(argv,text=True,stderr=subprocess.STDOUT,timeout=120).strip()


@contextmanager
def running_repository(gateway):
    with tempfile.TemporaryDirectory(prefix='lc4ri-build-') as directory:
        root=Path(directory)
        work=root/'work'
        marker_path='/etc/lc4ri-built'
        marker_value='built-by-repo2docker'
        registry='coursewarehub.test:5000'
        run(['git','init','-b','main',str(work)])
        (work/'Dockerfile').write_text('FROM '+registry+'/coursewarehub/initial-course-image:latest\n'
            'USER root\nRUN printf "'+marker_value+'\\n" > '+marker_path+'\nUSER jovyan\n')
        run(['git','-C',str(work),'add','Dockerfile'])
        run(['git','-C',str(work),'-c','user.name=CoursewareHub E2E','-c','user.email=e2e@example.test','commit','-m','Build runtime probe'])
        ref=run(['git','-C',str(work),'rev-parse','HEAD'])
        run(['git','clone','--bare',str(work),str(root/'course.git')])
        daemon=subprocess.Popen(['git','daemon','--reuseaddr','--export-all','--base-path='+directory,
            '--listen=0.0.0.0','--port=19418'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            url='git://'+gateway+':19418/course.git'
            for attempt in range(30):
                assert daemon.poll() is None,'Git fixture terminated'
                probe=subprocess.run(['git','ls-remote',url,'HEAD'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
                if probe.returncode==0:
                    assert probe.stdout.split()[0]==ref
                    break
                time.sleep(1)
            else:raise AssertionError('Git fixture did not become ready')
            yield dict(url=url,ref=ref,name='lc4ri-built',image=registry+'/lc4ri-built:'+ref[:7],
                marker_path=marker_path,marker_value=marker_value)
        finally:
            daemon.terminate()
            daemon.wait(timeout=10)


def wait_for_environment_ui(page):
    # RequireJS attaches the click handlers after the document has loaded.
    page.wait_for_function("""() => {
        const button=document.getElementById('add-environment');
        return button && window.jQuery && jQuery._data(button,'events')?.click?.length;
    }""")


def build_environment(page,base,repository,results,manager):
    results=Path(results)
    page.goto(base+'/services/environments/')
    wait_for_environment_ui(page)
    page.locator('#add-environment').click()
    dialog=page.locator('#create-environment-dialog')
    dialog.locator('.repo-input').fill(repository['url'])
    dialog.locator('.ref-input').fill(repository['ref'])
    dialog.locator('.name-input').fill(repository['name'])
    with page.expect_response(lambda r:'/services/environments/api/environments' in r.url and r.request.method=='POST',timeout=600000) as creating:
        dialog.locator('.save-button').click()
    assert creating.value.status==200,creating.value.text()
    name=repository['image'].split('/',1)[1]
    deadline=time.monotonic()+900
    # Wait for the asynchronous build/push to finish before refreshing the image list.
    while time.monotonic()<deadline:
        containers=manager.output(['docker','ps','-q','--filter','label=repo2docker.build='+name]).splitlines()
        if not containers:
            break
        for container in containers:
            logs=manager.run(['docker','logs',container],check=False).stdout
            (results/'repo2docker-build.log').write_text(logs)
        time.sleep(2)
    else:
        raise AssertionError('repo2docker build did not finish: '+name)
    response=page.goto(base+'/services/environments/')
    assert response is not None and response.status==200,page.locator('body').inner_text()
    wait_for_environment_ui(page)
    row=page.locator('tr.image-row[data-image="'+name+'"]')
    from playwright.sync_api import expect
    expect(row.locator('.status-col .fa-check-square')).to_be_visible()
    with page.expect_response(lambda r:'/api/environments/default-course-image' in r.url and r.request.method=='PUT',timeout=60000) as selecting:
        row.locator('.set-default-course-image').check()
    assert selecting.value.status==200,selecting.value.text()
    expect(row.locator('.set-default-course-image')).to_be_checked()
    page.screenshot(path=str(results/'repo2docker-built.png'),full_page=True)
    (results/'repo2docker-results.json').write_text(json.dumps(dict(repository=repository,
        build_and_registry_publication='passed',default_image_selection='passed'),indent=2)+'\n')
    print('RUNTIME PASS: repo2docker built the Git source and published the selected course image',flush=True)


def marker_check(repository):
    return 'assert Path('+repr(repository['marker_path'])+').read_text().strip() == '+repr(repository['marker_value'])+'\n'
