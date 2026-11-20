"""Expose the connection's peer address to notebooks on the CI guest network."""
from contextlib import contextmanager
from ipaddress import IPv4Address
import json
from threading import Thread
from wsgiref.simple_server import make_server


def lookup(environ, respond):
    if environ['PATH_INFO'] != '/json':
        respond('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'Unknown path']
    respond('200 OK', [('Content-Type', 'application/json')])
    return [json.dumps({'ip': environ['REMOTE_ADDR']}).encode()]


@contextmanager
def running_lookup(host):
    with make_server(host, 0, lookup) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f'http://{host}:{server.server_port}/json'
        finally:
            server.shutdown()
            thread.join()


def read_operator_ip(index, text, endpoint, edit, run):
    edit(index, text.replace('https://ifconfig.co/json', endpoint),
         'Look up the source address on the isolated CI network')
    observed = run(index)
    output = ''.join(item['text'] for item in observed['outputs'] if item['output_type'] == 'stream')
    return str(IPv4Address(output.strip()))
