"""Serve emulated CloudFormation/EC2 APIs backed by explicitly assigned CI guests."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
import hashlib
import shutil
import subprocess
from pathlib import Path
import re
from unittest.mock import patch

import boto3
import moto
from moto.core.responses import ActionResult
from moto.cloudformation.responses import CloudFormationResponse
from moto.cloudformation.exceptions import ValidationError
from moto.core.utils import utcnow
from moto.ec2.models import ec2_backends
from moto.ec2.models.elastic_ip_addresses import ElasticAddress
from moto.ec2.models.instances import Instance
from moto.ec2.models.security_groups import SecurityGroup
from moto.ec2.responses.general import General
from moto.ec2.utils import Base64EncodedString
from moto.server import ThreadedMotoServer


@dataclass
class GuestBinding:
    role: str
    guest: object
    console_path: Path
    instance_id: str | None = None


def role_from_tags(properties):
    name = next(tag['Value'] for tag in properties.get('Tags', []) if tag['Key'] == 'Name')
    if name.endswith('-OpHub'):
        return 'operationhub'
    if name.endswith('-CwHub-Master'):
        return 'master'
    match = re.search(r'-CwHub-Slave(\d+)$', name)
    if match:
        return 'worker' + match[1]
    raise ValueError('No CI guest is assigned to the requested instance: ' + name)


@contextmanager
def running_aws_provider(bindings, results, host, port=18502, region='ap-northeast-1'):
    if moto.__version__ != '5.2.4':
        raise RuntimeError('Review the Moto adapters before changing the pinned provider version')
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    by_role = {binding.role: binding for binding in bindings}
    assert len(by_role) == len(bindings)
    by_id, events = {}, []
    original_sg = SecurityGroup.create_from_cloudformation_json.__func__
    original_instance = Instance.create_from_cloudformation_json.__func__
    original_address = ElasticAddress.create_from_cloudformation_json.__func__
    original_console = General.get_console_output
    original_create_stack = CloudFormationResponse.create_stack

    def create_stack(response):
        body = response._get_param('TemplateBody')
        if body is None or response._get_param('TemplateURL'):
            raise ValidationError(message='The CI provider requires an inline template')
        digest = hashlib.sha256(body.encode()).hexdigest()
        path = results / ('cloudformation-' + digest[:12] + '.yaml')
        path.write_text(body)
        executable = shutil.which('cfn-lint')
        if executable is None:
            raise RuntimeError('cfn-lint must validate templates before the guest adapter runs')
        validation = subprocess.run([executable, '--non-zero-exit-code', 'error',
            '--template', str(path), '--regions', response.region], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        events.append({'action': 'validate_template', 'sha256': digest,
                       'status': 'passed' if validation.returncode == 0 else 'failed'})
        (results / ('cloudformation-' + digest[:12] + '.lint.txt')).write_text(validation.stdout)
        if validation.returncode:
            raise ValidationError(message='CloudFormation schema validation failed: ' + validation.stdout)
        return original_create_stack(response)

    def security_group(cls, name, resource, account, region, **kwargs):
        resource = deepcopy(resource)
        # AWS permits omitted ports for IpProtocol=-1; Moto's CFN adapter indexes them.
        for rule in resource.get('Properties', {}).get('SecurityGroupIngress', []):
            if str(rule.get('IpProtocol')) == '-1':
                rule.setdefault('FromPort', -1)
                rule.setdefault('ToPort', -1)
        return original_sg(cls, name, resource, account, region, **kwargs)

    def instance(cls, name, resource, account, region, **kwargs):
        resource = deepcopy(resource)
        properties = resource['Properties']
        binding = by_role[role_from_tags(properties)]
        if binding.instance_id is not None:
            raise ValueError('The CI guest is already bound to another instance: ' + binding.role)
        # Translate the single primary NIC into the fields understood by Moto's CFN adapter.
        interfaces = properties.pop('NetworkInterfaces', None)
        if interfaces:
            if len(interfaces) != 1 or int(interfaces[0]['DeviceIndex']) != 0:
                raise ValueError('The CI provider supports one primary network interface')
            properties['SubnetId'] = interfaces[0]['SubnetId']
            properties['SecurityGroups'] = interfaces[0]['GroupSet']
        if 'SecurityGroupIds' in properties:
            properties['SecurityGroups'] = properties.pop('SecurityGroupIds')
        properties['PrivateIpAddress'] = binding.guest.address
        model = original_instance(cls, name, resource, account, region, **kwargs)
        key = ec2_backends[account][region].key_pairs[properties['KeyName']].public_key.strip()
        if not re.fullmatch(r'ssh-(?:rsa|ed25519) [A-Za-z0-9+/=]+(?: [^\r\n]*)?', key):
            raise ValueError('Unsupported or invalid imported SSH public key')
        binding.guest.run(['sh', '-c',
            'install -d -m 700 -o centos -g centos /home/centos/.ssh && '
            'cat >> /home/centos/.ssh/authorized_keys && '
            'chown centos:centos /home/centos/.ssh/authorized_keys && '
            'chmod 600 /home/centos/.ssh/authorized_keys'], input=key + '\n')
        binding.instance_id = model.id
        by_id[model.id] = binding
        if interfaces and interfaces[0].get('AssociatePublicIpAddress'):
            model.nics[0].public_ip = binding.guest.address
        events.append({'action': 'bind_instance', 'role': binding.role, 'instance_id': model.id,
                       'address': binding.guest.address, 'key_name': properties['KeyName']})
        return model

    def address(cls, name, resource, account, region, **kwargs):
        model = original_address(cls, name, resource, account, region, **kwargs)
        instance_id = resource.get('Properties', {}).get('InstanceId')
        if instance_id:
            binding = by_id[instance_id]
            model.public_ip = binding.guest.address
            if model.eni is not None:
                model.eni.public_ip = binding.guest.address
        return model

    def console(response):
        instance_id = response._get_param('InstanceId')
        if instance_id not in by_id:
            return original_console(response)
        binding = by_id[instance_id]
        output = binding.console_path.read_text(errors='replace')
        events.append({'action': 'get_console_output', 'instance_id': instance_id, 'role': binding.role})
        return ActionResult({'InstanceId': instance_id, 'Timestamp': utcnow(),
                             'Output': Base64EncodedString.from_raw_string(output)})

    with ExitStack() as stack:
        stack.enter_context(patch.object(CloudFormationResponse, 'create_stack', create_stack))
        stack.enter_context(patch.object(SecurityGroup, 'create_from_cloudformation_json', classmethod(security_group)))
        stack.enter_context(patch.object(Instance, 'create_from_cloudformation_json', classmethod(instance)))
        stack.enter_context(patch.object(ElasticAddress, 'create_from_cloudformation_json', classmethod(address)))
        stack.enter_context(patch.object(General, 'get_console_output', console))
        server = ThreadedMotoServer(ip_address=host, port=port, verbose=False)
        server.start()
        stack.callback(server.stop)
        endpoint = 'http://' + host + ':' + str(port)
        session = boto3.Session(region_name=region, aws_access_key_id='testing', aws_secret_access_key='testing')
        ec2 = session.client('ec2', endpoint_url=endpoint)
        image_id = ec2.register_image(Name='CoursewareHub-CI-guest', Architecture='x86_64',
                                      RootDeviceName='/dev/sda1')['ImageId']
        try:
            yield {'endpoint': endpoint, 'image_id': image_id, 'region': region}
        finally:
            (results / 'aws-provider-results.json').write_text(json.dumps({
                'provider': 'moto', 'version': moto.__version__, 'events': events,
                'bindings': [{'role': b.role, 'instance_id': b.instance_id, 'address': b.guest.address}
                             for b in bindings]}, indent=2) + '\n')
