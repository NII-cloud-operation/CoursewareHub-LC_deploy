"""Operate the original AWS OperationHub construction procedure through LC4RI."""
from contextlib import contextmanager
from pathlib import Path
import sys
import json
import os
import re
import secrets
import subprocess
import time
from urllib.parse import quote

from notebook_controls import source
from lab_controls import BASE, open_document
from ip_lookup import running_lookup, read_operator_ip
from browser_evidence import BrowserRecording


def setup(args, results):
    from aws_guest_provider import GuestBinding, running_aws_provider
    from guest import Guest
    binding=GuestBinding('operationhub', Guest('operationhub',args.master,'ssh'), results/'operationhub-console.log')
    bindings=[binding]+[GuestBinding(role,Guest(role,address,'ssh'),results/(role+'-console.log'))
        for role,address in [('master','10.1.1.10'),('worker1','10.1.1.11'),('worker2','10.1.1.12')]]
    args.provider=args.fixtures.enter_context(running_aws_provider(bindings, results/'aws-api', os.environ['LC4RI_AWS_HOST'],port=18503))
    args.ip_endpoint = args.fixtures.enter_context(running_lookup(os.environ['LC4RI_AWS_HOST']))
    args.operation_password=secrets.token_hex(24)


def operate(original, args, controls, page, records, results):
    run, edit = controls.run, controls.edit
    select, state = controls.select, controls.state
    import boto3
    ec2=boto3.client('ec2',region_name='ap-northeast-1',aws_access_key_id='testing',aws_secret_access_key='testing',endpoint_url=args.provider['endpoint'])
    group='ci-operationhub'
    find = controls.find
    lookup = find('https://ifconfig.co/json')
    stack = find("cloudformation_stack_name = 'ophubtest0001'")
    prefix = find("resource_prefix = 'ophubtest'")
    image = find('\nimage_id = ')
    confirmations = controls.find_all('assert False', 2)
    ssh = find('SSHに関する許可が含まれていれば先に進める')
    https = find('HTTPSに関する許可が含まれていれば先に進める')
    hosts_diff = find('hosts-backup ./hosts')
    password = find('admin_password = getpass.getpass()')
    user_image = find('singleuser-image docker build -t {image_name}')
    for index,cell in enumerate(original['cells']):
        text=source(cell)
        if cell['cell_type']!='code' or not text.strip():continue
        if index == lookup:
            operator_ip = read_operator_ip(index, text, args.ip_endpoint, edit, run)
            continue
        if index == stack:
            edit(index,text.replace('ophubtest0001',group),'Set the test CloudFormation stack name')
        if index == prefix:
            edit(index,text.replace('ophubtest','ci-operationhub'),'Set the test resource prefix')
        if index == image:
            edit(index,re.sub(r'^image_id = .+$','image_id = '+repr(args.provider['image_id']),text,flags=re.M),'Select the AMI registered by the CI guest provider')
        if index in confirmations:
            select(index)
            page.get_by_title('freeze selected cells', exact=True).click()
            assert state(index)['frozen']
            records.append(dict(notebook='OperationHub',cell=index,action='confirm inputs and Freeze via toolbar'))
            continue
        # The procedure asks the operator to add rules using the AWS console.
        # Use the emulated EC2 API for that external action; the next original cell verifies it.
        if index in (ssh, https):
            groups=ec2.describe_security_groups(Filters=[{'Name':'tag:aws:cloudformation:stack-name','Values':[group]}])['SecurityGroups']
            if not groups:
                groups=[g for g in ec2.describe_security_groups()['SecurityGroups'] if g.get('GroupName','')!='default']
            assert len(groups)==1, groups
            port=443 if index == https else 22
            ec2.authorize_security_group_ingress(GroupId=groups[0]['GroupId'],IpPermissions=[dict(IpProtocol='tcp',FromPort=port,ToPort=port,IpRanges=[{'CidrIp':operator_ip+'/32'}])])
            records.append(dict(notebook='OperationHub',action='add operator-required EC2 ingress rule',port=port))
        if index == hosts_diff:
            expected=f'[{group}]\n{args.master} ansible_ssh_user=centos ansible_ssh_private_key_file=~/.ssh/ansible_id_rsa\n'
            editor=page.context.new_page()
            open_document(editor, BASE, os.environ['LC4RI_TEST_TOKEN'], 'hosts')
            editor.locator('.jp-FileEditor .cm-content').click()
            editor.keyboard.press('Control+A')
            editor.keyboard.insert_text(expected)
            assert editor.evaluate('window.jupyterapp.shell.currentWidget.context.model.toString()') == expected
            editor.keyboard.press('Control+s')
            import urllib.request
            for attempt in range(100):
                request=urllib.request.Request(BASE+'/api/contents/hosts',headers={'Authorization':'token '+os.environ['LC4RI_TEST_TOKEN']})
                with urllib.request.urlopen(request, timeout=30) as response:
                    if json.load(response)['content']==expected:break
                editor.wait_for_timeout(100)
            else:raise AssertionError('Edited hosts file was not saved')
            editor.close()
            records.append(dict(notebook='OperationHub',action='edit hosts through the file editor',after=expected))
        inputs=(args.operation_password,) if index == password else ()
        if index == user_image:
            observed = run(index, timeout_ms=45 * 60 * 1000)
        else:
            observed = run(index, inputs=inputs)
        output=json.dumps(observed['outputs'],ensure_ascii=False)
        if re.search(r'FAILED!|UNREACHABLE!|failed=[1-9]',output):
            raise AssertionError(f'OperationHub cell {index}: operation failed: {output[-3000:]}')



@contextmanager
def working_notebook(admin, filename, evidence):
    page, results = admin.page, admin.results
    fetch, base, prefix = admin.fetch, admin.base, admin.prefix
    from notebook_controls import operations
    from playwright.sync_api import expect
    page.goto(base + prefix + 'tree')
    expect(page.get_by_text(filename, exact=True)).to_be_visible()
    original = json.loads((Path(__file__).resolve().parents[2] / 'coursewarehub' / filename).read_text())
    recipe = fetch(prefix + 'api/contents/' + quote(filename))['content']
    assert [source(c) for c in recipe['cells']] == [source(c) for c in original['cells']]
    records=[]
    copied=None
    path=None
    try:
        page.goto(base+prefix+'notebooks/'+quote(filename))
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        page.locator('#menubar .dropdown-toggle').filter(has_text=re.compile(r'^File$')).click()
        with page.context.expect_page() as opened:
            page.locator('#copy_notebook').click()
        copied=opened.value
        copied.set_default_timeout(60000)
        copied.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        from urllib.parse import unquote
        path=unquote(copied.url.split('/notebooks/',1)[1].split('?',1)[0])
        assert path != filename
        assert copied.evaluate('()=>Jupyter.notebook.get_cells().map(c=>c.get_text())')==[source(c) for c in original['cells']]
        controls=operations(copied,filename,original,records,timeout=900000)
        yield original, copied, controls, records
        copied.keyboard.press('Control+s')
        copied.wait_for_function('!Jupyter.notebook.dirty')
        saved=fetch(prefix+'api/contents/'+quote(path))['content']
        count=len(original['cells'])
        assert len(saved['cells']) in (count,count+1)
        if len(saved['cells'])>count:
            extra=saved['cells'][-1]
            assert extra['cell_type']=='code' and source(extra)=='' and not extra.get('outputs')
        for index,cell in enumerate(saved['cells'][:count]):
            assert source(cell)==controls.edits.get(index,source(original['cells'][index])),index
        (results/f'{evidence}-working.ipynb').write_text(json.dumps(saved,ensure_ascii=False,indent=1))
        deployed=fetch(prefix+'api/contents/'+quote(filename))['content']
        assert [source(c) for c in deployed['cells']]==[source(c) for c in original['cells']]
        copied.screenshot(path=str(results/f'{evidence}-completed.png'),full_page=True)
    finally:
        if sys.exc_info()[1] is not None:
            if copied is not None:
                copied.screenshot(path=str(results/f'{evidence}-failure.png'),full_page=True)
                saved=copied.evaluate('()=>Jupyter.notebook.toJSON()')
                (results/f'{evidence}-failure.ipynb').write_text(json.dumps(saved,ensure_ascii=False,indent=1))
        (results/f'{evidence}-operations.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        if copied is not None:copied.close()


class OperationHubAdmin:
    prefix = '/user/ohadmin/'

    def __init__(self, args, results, page):
        self.args = args
        self.results = results
        self.page = page
        self.base = 'https://' + args.master

    def log_in_and_start_server(self):
        page, base = self.page, self.base
        page.goto(base+'/hub/login')
        page.locator('#username_input').fill('ohadmin')
        page.locator('#password_input').fill(self.args.operation_password)
        page.locator('#login_submit').click()
        page.wait_for_url(lambda url:'/hub/login' not in str(url),timeout=60000)
        page.goto(base+'/hub/home')
        page.locator('#start').click()
        page.wait_for_url('**/user/ohadmin/**',timeout=600000)
        page.wait_for_function('window.Jupyter && Jupyter.notebook_list',timeout=120000)
        page.screenshot(path=str(self.results/'operationhub-tree.png'),full_page=True)

    def fetch(self, path,method='GET',body=None):
        value=self.page.evaluate("""async ({path,method,body})=>{
            const c=document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
            const r=await fetch(path,{method,signal:AbortSignal.timeout(30000),headers:{'Content-Type':'application/json','X-XSRFToken':c?decodeURIComponent(c[1]):''},body:body===null?undefined:JSON.stringify(body)});
            return {status:r.status,text:await r.text()};}""",dict(path=path,method=method,body=body))
        assert 200<=value['status']<300,value
        return json.loads(value['text']) if value['text'] else None

    def run_user_kernel(self):
        from playwright.sync_api import expect
        page, base, prefix = self.page, self.base, self.prefix
        notebook={'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'name':'python3','display_name':'Python 3','language':'python'}},
            'cells':[{'cell_type':'code','id':'runtime-check','metadata':{},'execution_count':None,'outputs':[],'source':''}]}
        self.fetch(prefix+'api/contents/lc4ri-operationhub.ipynb','PUT',{'type':'notebook','format':'json','content':notebook})
        page.goto(base+prefix+'notebooks/lc4ri-operationhub.ipynb')
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        page.locator('.cell').first.locator('.CodeMirror').click()
        page.keyboard.insert_text('import os\nassert os.getuid() > 0\nprint("OPERATIONHUB_RESULT", 6 * 7)')
        page.keyboard.press('Shift+Enter')
        expect(page.locator('.cell').first.locator('.output')).to_contain_text('OPERATIONHUB_RESULT 42',timeout=120000)
        page.keyboard.press('Control+s')
        page.wait_for_function('!Jupyter.notebook.dirty')
        (self.results/'operationhub-kernel.ipynb').write_text(json.dumps(self.fetch(prefix+'api/contents/lc4ri-operationhub.ipynb')['content'],ensure_ascii=False,indent=1))

    def run_aws_vm_notebook(self, filename, group):
        page, fetch, prefix, base = self.page, self.fetch, self.prefix, self.base
        with working_notebook(self, filename, 'operationhub-aws') as (original, copied, controls, records):
            find = controls.find
            lookup = find('https://ifconfig.co/json')
            image = find('\nimage_id = ')
            confirmation = find('assert False')
            ssh = find('SSHに関する許可が含まれていれば先に進める')
            internal = find('全てのトラフィックを自身のセキュリティグループ')
            hosts_diff = find('hosts-backup ./hosts')
            generated = find("print('[{}]'.format(cloudformation_stack_name))")
            pings = controls.find_all('!ansible -m ping', 2)
            import boto3
            ec2=boto3.client('ec2',region_name='ap-northeast-1',aws_access_key_id='testing',aws_secret_access_key='testing',endpoint_url=self.args.provider['endpoint'])
            inventory=None
            ping_outputs=[]
            for index,cell in enumerate(original['cells']):
                text=source(cell)
                if cell['cell_type']!='code' or not text.strip():continue
                if index == lookup:
                    operator_ip = read_operator_ip(index, text, self.args.ip_endpoint, controls.edit, controls.run)
                    continue
                if index == image:
                    controls.edit(index,re.sub(r'^image_id = .+$','image_id = '+repr(self.args.provider['image_id']),text,flags=re.M),'Select the registered guest AMI')
                if index == confirmation:
                    controls.select(index)
                    copied.locator('button:has(.fa-freeze)').click()
                    assert controls.state(index)['frozen']
                    records.append(dict(notebook='CoursewareHub-AWS-D01',cell=index,action='confirm inputs and Freeze via toolbar'))
                    continue
                if index in (ssh, internal):
                    groups=ec2.describe_security_groups(Filters=[{'Name':'tag:aws:cloudformation:stack-name','Values':[group]}])['SecurityGroups']
                    assert len(groups)==1,groups
                    sg=groups[0]['GroupId']
                    if index == ssh:
                        permissions=[dict(IpProtocol='tcp',FromPort=22,ToPort=22,IpRanges=[{'CidrIp':operator_ip+'/32'}])]
                    else:
                        permissions=[dict(IpProtocol='-1',UserIdGroupPairs=[{'GroupId':sg}])]
                    ec2.authorize_security_group_ingress(GroupId=sg,IpPermissions=permissions)
                    records.append(dict(notebook='CoursewareHub-AWS-D01',action='add operator-required EC2 ingress rule',permissions=permissions))
                if index == hosts_diff:
                    assert inventory is not None
                    previous=fetch(prefix+'api/contents/hosts')['content']
                    expected=previous.rstrip()+'\n\n'+inventory
                    editor=page.context.new_page()
                    editor.goto(base+prefix+'edit/hosts')
                    editor.locator('.CodeMirror').click()
                    editor.keyboard.press('Control+A')
                    editor.keyboard.insert_text(expected)
                    assert editor.locator('.CodeMirror').evaluate('e=>e.CodeMirror.getValue()')==expected
                    editor.keyboard.press('Control+s')
                    for attempt in range(100):
                        if fetch(prefix+'api/contents/hosts')['content']==expected:break
                        editor.wait_for_timeout(100)
                    else:raise AssertionError('Edited OperationHub inventory was not saved')
                    editor.close()
                    records.append(dict(notebook='CoursewareHub-AWS-D01',action='copy generated inventory through the file editor',after=expected))
                observed=controls.run(index)
                output=json.dumps(observed['outputs'],ensure_ascii=False)
                assert not re.search(r'FAILED!|UNREACHABLE!|failed=[1-9]',output),(index,output[-3000:])
                if index == generated:
                    inventory=''.join(o.get('text','') for o in observed['outputs'] if o['output_type']=='stream')
                    inventory='\n'.join(inventory.splitlines())+'\n'
                    assert '['+group+']' in inventory and all(address in inventory for address in ('10.1.1.10','10.1.1.11','10.1.1.12')),inventory
                if index in pings:
                    ping_outputs.append(output)
        (self.results / 'operationhub-aws-hosts').write_text(fetch(prefix + 'api/contents/hosts')['content'])
        return ping_outputs

    def run_construction_notebook(self, filename, procedure, deployment):
        evidence = 'operationhub-' + filename.split('_', 1)[0].lower()
        with working_notebook(self, filename, evidence) as (original, page, controls, records):
            procedure(original, deployment, controls, page, records, self.results)

    def verify_inventory_roles(self, group, master, workers):
        from configparser import ConfigParser
        inventory = self.fetch(self.prefix + 'api/contents/' + group + '_inventory')['content']
        (self.results / 'operationhub-d02-inventory').write_text(inventory)
        sections = ConfigParser(allow_no_value=True, delimiters=(' ',), interpolation=None)
        sections.read_string(inventory)
        assert set(sections['cwhub_master']) == {master}
        assert set(sections['cwhub_nfs_server']) == {master}
        assert set(sections['cwhub_nodes']) == set(workers)


@contextmanager
def operationhub_admin_session(args, results, browser):
    videos=results/'videos'/'operationhub-runtime'
    recording = BrowserRecording(browser, videos, "OperationHub-login-kernel-and-AWS-D01-D02")
    context = recording.context
    context.tracing.start(screenshots=True,snapshots=True,sources=True)
    page=context.new_page()
    try:
        yield OperationHubAdmin(args, results, page)
        (results/'operationhub-runtime.json').write_text(json.dumps({'login':'passed','user_server':'passed','construction_notebooks_deployed':'passed','user_kernel':'passed','aws_guest_provisioning_and_real_ssh':'passed','inventory_roles':'passed'}))
    finally:
        if sys.exc_info()[1] is not None:
            page.screenshot(path=str(results/'operationhub-runtime-failure.png'),full_page=True)
        recording.finish(results/'operationhub-runtime-trace.zip')
