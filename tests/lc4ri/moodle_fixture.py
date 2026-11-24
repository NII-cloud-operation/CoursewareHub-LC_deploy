"""Disposable Moodle installation with a registered CoursewareHub LTI 1.3 activity."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

MOODLE_REF = '215f44380bdc3ccf852a3a22e34f8423ff64033a'  # Moodle v4.5.15.


def run(argv, *, input=None, timeout=300):
    result = subprocess.run(argv, input=input, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Moodle fixture command failed: ' + result.stdout)
    return result.stdout


class Moodle:
    def __init__(self, url, password, metadata):
        self.url, self.password, self.metadata = url, password, metadata

    @property
    def registration(self):
        return {self.url: dict(client_id=self.metadata['client_id'],
            auth_login_url=self.url + '/mod/lti/auth.php',
            auth_token_url=self.url + '/mod/lti/token.php',
            key_set_url=self.url + '/mod/lti/certs.php', private_key_file='/private.key',
            deployment=[self.metadata['deployment_id']])}

    def register_tool_key(self, manager):
        public = manager.output(['openssl', 'pkey', '-pubout', '-in', '/etc/jupyterhub/lti/private.key'])
        assert public.startswith('-----BEGIN PUBLIC KEY-----'), public
        run(['docker', 'exec', '-i', '-u', 'www-data', 'cwh-moodle', 'php', '/tmp/setup.php',
             'public-key', self.metadata['deployment_id']], input=public)


@contextmanager
def running_moodle(gateway, network, results):
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='cwh-moodle-'))
    source = directory / 'moodle'
    password = secrets.token_hex(24)
    url = 'http://' + gateway + ':18502'
    repo = Path(__file__).resolve().parent
    try:
        run(['git', 'clone', '--depth', '1', '--branch', 'v4.5.15',
             'https://github.com/moodle/moodle.git', str(source)])
        assert run(['git', '-C', str(source), 'rev-parse', 'HEAD']).strip() == MOODLE_REF
        shutil.copyfile(repo / 'moodle/config.php', source / 'config.php')
        env = dict(os.environ, CWH_MOODLE_PASSWORD=password, CWH_MOODLE_URL=url)
        # docker -e NAME takes its value from the caller environment.
        with patch.dict(os.environ, env):
            run(['docker', 'run', '-d', '--name', 'cwh-moodle-db', '--network', network,
                 '-e', 'POSTGRES_USER=moodle', '-e', 'POSTGRES_DB=moodle',
                 '-e', 'POSTGRES_PASSWORD=' + password, 'postgres:16'])
            for attempt in range(90):
                probe = subprocess.run(['docker', 'exec', 'cwh-moodle-db', 'pg_isready', '-U', 'moodle'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if probe.returncode == 0:
                    break
                time.sleep(2)
            else:
                raise AssertionError('Moodle PostgreSQL did not become ready')
            run(['docker', 'run', '-d', '--name', 'cwh-moodle', '--network', network,
                 '-p', '18502:80', '-e', 'CWH_MOODLE_PASSWORD', '-e', 'CWH_MOODLE_URL',
                 '-v', str(source) + ':/var/www/html',
                 '-v', str(repo / 'moodle/setup.php') + ':/tmp/setup.php:ro',
                 'moodlehq/moodle-php-apache:8.3'])
            run(['docker', 'exec', 'cwh-moodle', 'sh', '-c',
                 'mkdir -p /var/www/moodledata && chown www-data:www-data /var/www/moodledata && chmod 0770 /var/www/moodledata'])
            run(['docker', 'exec', 'cwh-moodle', 'php', '-l', '/tmp/setup.php'])
            install = run(['docker', 'exec', '-u', 'www-data', 'cwh-moodle', 'sh', '-c',
                 'php admin/cli/install_database.php --agree-license --adminuser=admin '
                 '--adminpass="$CWH_MOODLE_PASSWORD" --adminemail=admin@example.test '
                 '--fullname="CoursewareHub LTI E2E" --shortname=cwh-lti'], timeout=600)
            (results / 'installation.log').write_text(install)
            output = run(['docker', 'exec', '-u', 'www-data', 'cwh-moodle', 'php', '/tmp/setup.php'])
            (results / 'setup.log').write_text(output)
            metadata = json.loads(output.strip().splitlines()[-1])
            (results / 'registration.json').write_text(json.dumps(dict(source_ref=MOODLE_REF,
                issuer=url, **metadata), indent=2) + '\n')
            print('MOODLE PLATFORM PASS: installed Moodle 4.5.15 and registered the LTI tool, course, teacher and student', flush=True)
            yield Moodle(url, password, metadata)
    finally:
        for container in ['cwh-moodle', 'cwh-moodle-db']:
            logs = subprocess.run(['docker', 'logs', container], text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=20)
            (results / (container + '.log')).write_text(logs.stdout)
            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        shutil.rmtree(directory)
