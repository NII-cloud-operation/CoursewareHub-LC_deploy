"""Construction notebooks on the controller, followed by use of the deployed Hubs."""
import os
from types import SimpleNamespace

from construction import (
    launch_moodle_as_teacher_and_student, operate_d01, operate_d02, operate_d03,
    operate_d04, operate_d05, operate_o01, verify_inventory,
)
from runtime_ui import admin_notebook_session


def log_in_and_run_admin_notebooks_on_coursewarehub(args, results, browser, *, after_update=False):
    with admin_notebook_session(args, results, browser) as admin:
        admin.log_in()
        if args.build_repository and not after_update:
            admin.build_default_image()
        admin.start_user_server()
        if after_update:
            admin.verify_saved_file()
        admin.run_kernel_and_save_file()

        admin.trust_cluster_hosts()
        admin.run_notebook('D06_CoursewareHubコンテンツの配備.ipynb')
        admin.verify_deployed_notebook('admin_tools/00_GuidanceForTeacher.ipynb')

        admin.stop_user_server()
        admin.start_user_server()
        admin.verify_saved_file()


def run_local_coursewarehub(run_notebook, args, results, browser, api):
    from custom_image import running_repository

    run_notebook('coursewarehub/D01_オンプレミス環境の場合のマシン設定.ipynb', operate_d01)
    run_notebook('coursewarehub/D02_CoursewareHubインベントリの準備.ipynb', operate_d02)
    verify_inventory(args, results, api)
    run_notebook('coursewarehub/D03_NFSの準備.ipynb', operate_d03)
    run_notebook('coursewarehub/D04_Docker Swarmの準備.ipynb', operate_d04)
    run_notebook('coursewarehub/D05_CoursewareHubの準備.ipynb', operate_d05)

    args.build_repository = args.fixtures.enter_context(running_repository(os.environ['LC4RI_GATEWAY']))
    before = results / 'before-update'
    log_in_and_run_admin_notebooks_on_coursewarehub(args, before, browser)
    launch_moodle_as_teacher_and_student(args, before, browser)

    run_notebook('coursewarehub/O01_CoursewareHubの更新.ipynb', operate_o01)
    after = results / 'after-update'
    log_in_and_run_admin_notebooks_on_coursewarehub(args, after, browser, after_update=True)
    launch_moodle_as_teacher_and_student(args, after, browser, after_update=True)


def run_federated_coursewarehub(run_notebook, args, results, browser, api):
    from guest import Guest
    from saml_provider import prepare_saml, check_saml_login

    run_notebook('coursewarehub/D01_オンプレミス環境の場合のマシン設定.ipynb', operate_d01)
    run_notebook('coursewarehub/D02_CoursewareHubインベントリの準備.ipynb', operate_d02)
    verify_inventory(args, results, api)
    run_notebook('coursewarehub/D03_NFSの準備.ipynb', operate_d03)
    run_notebook('coursewarehub/D04_Docker Swarmの準備.ipynb', operate_d04)
    run_notebook('coursewarehub/D05_CoursewareHubの準備.ipynb', operate_d05)

    manager = Guest('master', args.master, 'ssh')
    before = results / 'before-update'
    prepare_saml(args.saml, manager, before / 'saml')
    log_in_and_run_admin_notebooks_on_coursewarehub(args, before, browser)
    check_saml_login(before / 'saml', args.saml, args.teacher_username, browser)

    run_notebook('coursewarehub/O01_CoursewareHubの更新.ipynb', operate_o01)
    after = results / 'after-update'
    prepare_saml(args.saml, manager, after / 'saml')
    log_in_and_run_admin_notebooks_on_coursewarehub(args, after, browser, after_update=True)
    check_saml_login(after / 'saml', args.saml, args.teacher_username, browser)


def run_operationhub(run_notebook, args, results, browser):
    from operationhub import operate, operationhub_admin_session

    run_notebook('D01_OperationHubをAWSに構築.ipynb', operate, evidence_name='OperationHub')
    with operationhub_admin_session(args, results, browser) as admin:
        admin.log_in_and_start_server()
        admin.run_user_kernel()
        group = 'cwhtest0001'
        master = '10.1.1.10'
        workers = ['10.1.1.11', '10.1.1.12']
        ping_outputs = admin.run_aws_vm_notebook('D01_CoursewareHub用VMをAWSに作成.ipynb', group)
        assert ping_outputs and all(output.count('SUCCESS') == 1 + len(workers) for output in ping_outputs), ping_outputs
        deployment = SimpleNamespace(master=master, workers=workers, group=group, saml=None, hub_image=None)
        admin.run_construction_notebook('D02_CoursewareHubインベントリの準備.ipynb', operate_d02, deployment)
        admin.verify_inventory_roles(group, master, workers)
