"""Local SAML IdP and attribute authority with real signed protocol responses."""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.parse import parse_qs, urlencode, urlparse
import uuid
import zlib

from lxml import etree
from signxml import XMLSigner, methods
from browser_evidence import BrowserRecording


NS = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion",
      "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
      "md": "urn:oasis:names:tc:SAML:2.0:metadata",
      "ds": "http://www.w3.org/2000/09/xmldsig#",
      "soap": "http://schemas.xmlsoap.org/soap/envelope/"}
BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:"
MAIL = "urn:oid:0.9.2342.19200300.100.1.3"
EPPN = "urn:oid:1.3.6.1.4.1.5923.1.1.1.6"
GROUP = "urn:oid:1.3.6.1.4.1.5923.1.5.1.1"


def sub(parent, prefix, name, text=None, **attributes):
    element = etree.SubElement(parent, "{" + NS[prefix] + "}" + name, **attributes)
    element.text = text
    return element


def instant(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def identifier():
    return "_" + uuid.uuid4().hex


def parse_xml(value):
    return etree.fromstring(value, parser=etree.XMLParser(resolve_entities=False, no_network=True))


class SAMLProvider:
    authorized_group = "urn:coursewarehub:e2e:teachers"

    def __init__(self, base_url, tool_url, key, cert, email):
        self.base_url = base_url.rstrip("/")
        self.tool_url = tool_url.rstrip("/")
        self.entity_id = self.base_url + "/metadata"
        self.authority_id = "https://" + urlparse(base_url).netloc + "/idp/shibboleth"
        self.sp_entity = self.tool_url + "/shibboleth-sp"
        self.acs = None
        self.key, self.cert, self.email = key, cert, email
        self.requests = []

    def metadata(self):
        root = etree.Element("{" + NS['md'] + "}EntitiesDescriptor", nsmap=NS)
        certificate = "".join(line for line in self.cert.decode().splitlines() if not line.startswith("---"))
        for entity_id, role in [(self.entity_id, "IDPSSODescriptor"), (self.authority_id, "AttributeAuthorityDescriptor")]:
            entity = sub(root, "md", "EntityDescriptor", entityID=entity_id)
            descriptor = sub(entity, "md", role, protocolSupportEnumeration=NS['samlp'])
            key = sub(descriptor, "md", "KeyDescriptor", use="signing")
            key_info = sub(key, "ds", "KeyInfo")
            sub(sub(key_info, "ds", "X509Data"), "ds", "X509Certificate", certificate)
            if role == "IDPSSODescriptor":
                sub(descriptor, "md", "SingleSignOnService", Binding=BINDING + "HTTP-Redirect", Location=self.base_url + "/sso")
            else:
                sub(descriptor, "md", "AttributeService", Binding=BINDING + "SOAP", Location=self.base_url + "/attributes")
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    def sign(self, element):
        return XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha256",
            digest_algorithm="sha256", c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#").sign(
                element, key=self.key, cert=self.cert, reference_uri=element.get("ID"))

    def response(self, request_id, *, attribute_query=False, subject=None, subject_format=None):
        if not attribute_query and self.acs is None:
            raise RuntimeError("SP metadata has not supplied an assertion consumer endpoint")
        issuer = self.authority_id if attribute_query else self.entity_id
        response = etree.Element("{" + NS['samlp'] + "}Response", nsmap=NS,
            ID=identifier(), Version="2.0", IssueInstant=instant(), InResponseTo=request_id)
        if not attribute_query:
            response.set("Destination", self.acs)
        sub(response, "saml", "Issuer", issuer)
        sub(response, "ds", "Signature", Id="placeholder")
        sub(sub(response, "samlp", "Status"), "samlp", "StatusCode", Value="urn:oasis:names:tc:SAML:2.0:status:Success")
        assertion = etree.Element("{" + NS['saml'] + "}Assertion", nsmap=NS,
            ID=identifier(), Version="2.0", IssueInstant=instant())
        sub(assertion, "saml", "Issuer", issuer)
        sub(assertion, "ds", "Signature", Id="placeholder")
        entity_subject = sub(assertion, "saml", "Subject")
        sub(entity_subject, "saml", "NameID", subject or self.email,
            Format=subject_format or "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress")
        expires = instant(300)
        if not attribute_query:
            confirmation = sub(entity_subject, "saml", "SubjectConfirmation", Method="urn:oasis:names:tc:SAML:2.0:cm:bearer")
            sub(confirmation, "saml", "SubjectConfirmationData", Recipient=self.acs,
                InResponseTo=request_id, NotOnOrAfter=expires)
        conditions = sub(assertion, "saml", "Conditions", NotBefore=instant(-60), NotOnOrAfter=expires)
        sub(sub(conditions, "saml", "AudienceRestriction"), "saml", "Audience",
            self.sp_entity)
        if not attribute_query:
            authn = sub(assertion, "saml", "AuthnStatement", AuthnInstant=instant(), SessionIndex=identifier())
            sub(sub(authn, "saml", "AuthnContext"), "saml", "AuthnContextClassRef",
                "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport")
        attributes = sub(assertion, "saml", "AttributeStatement")
        if attribute_query:
            values = {GROUP: self.authorized_group}
        else:
            values = {MAIL: self.email, EPPN: self.email}
        for name, value in values.items():
            attribute = sub(attributes, "saml", "Attribute", Name=name, NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:uri")
            sub(attribute, "saml", "AttributeValue", value)
        response.append(self.sign(assertion))
        return self.sign(response)

    def handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/metadata":
                    self.respond(200, provider.metadata(), "application/samlmetadata+xml")
                    return
                if url.path != "/sso":
                    self.respond(404, b"Unknown IdP endpoint", "text/plain")
                    return
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                request = parse_xml(zlib.decompress(base64.b64decode(query['SAMLRequest']), -15))
                assert request.tag == "{" + NS['samlp'] + "}AuthnRequest"
                assert request.findtext('saml:Issuer', namespaces=NS) == provider.sp_entity
                assert request.get('AssertionConsumerServiceURL', provider.acs) == provider.acs
                provider.requests.append({"endpoint": "sso"})
                fields = {"SAMLResponse": base64.b64encode(etree.tostring(provider.response(request.get('ID')))).decode(),
                          "RelayState": query.get('RelayState', '')}
                inputs = ''.join('<input type="hidden" name="' + escape(key, quote=True) +
                    '" value="' + escape(value, quote=True) + '">' for key, value in fields.items())
                html = '<!doctype html><form method="post" action="' + escape(provider.acs, quote=True) + '">' + inputs + '</form><script>document.forms[0].submit()</script>'
                self.respond(200, html.encode(), "text/html")

            def do_POST(self):
                if self.path != '/attributes':
                    self.respond(404, b"Unknown authority endpoint", "text/plain")
                    return
                size = int(self.headers.get('Content-Length', '0'))
                assert 0 < size <= 1048576
                envelope = parse_xml(self.rfile.read(size))
                query = envelope.find('soap:Body/samlp:AttributeQuery', NS)
                assert query is not None
                assert query.findtext('saml:Issuer', namespaces=NS) == provider.sp_entity
                subject = query.find('saml:Subject/saml:NameID', NS)
                assert subject is not None and subject.text == provider.email
                provider.requests.append({"endpoint": "attributes"})
                response = provider.response(query.get('ID'), attribute_query=True,
                    subject=subject.text, subject_format=subject.get('Format'))
                soap = etree.Element('{' + NS['soap'] + '}Envelope', nsmap=NS)
                sub(soap, 'soap', 'Body').append(response)
                self.respond(200, etree.tostring(soap), 'text/xml')

            def respond(self, status, data, content_type):
                self.send_response(status)
                self.send_header('Content-Type', content_type + '; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass
        return Handler


@contextmanager
def running_provider(base_url, tool_url, email):
    with tempfile.TemporaryDirectory(prefix='cwh-saml-') as directory:
        key, cert = Path(directory) / 'key.pem', Path(directory) / 'cert.pem'
        subprocess.run(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-x509',
            '-days', '2', '-subj', '/CN=CoursewareHub E2E IdP', '-keyout', str(key), '-out', str(cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        key.chmod(0o600)
        provider = SAMLProvider(base_url, tool_url, key.read_bytes(), cert.read_bytes(), email)
        server = ThreadingHTTPServer(('0.0.0.0', urlparse(base_url).port), provider.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield provider
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def prepare_saml(provider, manager, results):
    import ssl
    import urllib.request
    results=Path(results)
    results.mkdir(parents=True,exist_ok=True)
    certificate=manager.output(['cat','/etc/jupyterhub/nginx/certs/server.cer'])
    tls=ssl.create_default_context(cadata=certificate)
    try:
        with urllib.request.urlopen(provider.tool_url+'/simplesaml/module.php/saml/sp/metadata.php/default-sp',context=tls,timeout=30) as response:
            metadata=response.read()
    except urllib.error.HTTPError as error:
        (results/'sp-metadata-error.html').write_bytes(error.read())
        raise
    (results/'sp-metadata.xml').write_bytes(metadata)
    entity=parse_xml(metadata)
    assert entity.get('entityID')==provider.sp_entity,entity.get('entityID')
    endpoints=entity.findall('md:SPSSODescriptor/md:AssertionConsumerService',NS)
    provider.acs=next(e.get('Location') for e in endpoints if e.get('Binding')==BINDING+'HTTP-POST')


def launch_saml(page,provider):
    page.goto(provider.tool_url+'/php/sp.php')
    discovery=parse_qs(urlparse(page.url).query)
    if 'return' in discovery and 'returnIDParam' in discovery:
        target=discovery['return'][0]
        location,origin=urlparse(target),urlparse(provider.tool_url)
        assert (location.scheme,location.netloc)==(origin.scheme,origin.netloc),target
        separator='&' if '?' in target else '?'
        page.goto(target+separator+urlencode({discovery['returnIDParam'][0]:provider.entity_id}))
    elif page.locator('select[name="idpentityid"]').count():
        page.locator('select[name="idpentityid"]').select_option(provider.entity_id)
        page.locator('button[type="submit"]').click()


def check_saml_login(results, provider, expected_username, browser):
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    before = len(provider.requests)
    recording = BrowserRecording(browser, results / 'videos' / 'valid', results.parent.name + '-SAML-valid')
    context = recording.context
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    page.set_default_timeout(60000)
    record = dict(case='valid', status='running')
    try:
        launch_saml(page, provider)
        page.wait_for_function("location.pathname.startsWith('/hub/') || location.pathname.startsWith('/user/')", timeout=120000)
        page.goto(provider.tool_url + '/hub/home')
        user=page.evaluate("""async()=>{
            const cookie=document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
            const token=(window.jhdata && jhdata.xsrf_token) || (cookie ? decodeURIComponent(cookie[1]):'');
            const r=await fetch('/hub/api/user',{headers:{'X-XSRFToken':token}});
            return {status:r.status,text:await r.text()};
        }""")
        assert user['status']==200 and json.loads(user['text'])['name']==expected_username,user
        requests = provider.requests[before:]
        assert {'endpoint': 'sso'} in requests and {'endpoint': 'attributes'} in requests, requests
        page.screenshot(path=str(results / 'valid.png'), full_page=True)
        record['status'] = 'passed'
        print('SAML PASS: valid', flush=True)
    finally:
        error = sys.exc_info()[1]
        if error is not None:
            record.update(status='failed', error=str(error))
            page.screenshot(path=str(results / 'valid-failure.png'), full_page=True)
            (results / 'valid-failure.html').write_text(page.content())
        (results / 'saml-results.json').write_text(json.dumps([record], indent=2) + '\n')
        (results / 'provider-requests.json').write_text(json.dumps(provider.requests[before:], indent=2) + '\n')
        recording.finish(results / 'valid-trace.zip')


def federation_inputs(find, provider):
    settings = [
        ("group_vars['enable_test_federation'] = False", "group_vars['enable_test_federation'] = True"),
        ("group_vars['enable_federation'] = False", "group_vars['enable_federation'] = True"),
        ("# group_vars['cg_fqdn'] = 'sptest.cg.gakunin.jp'\n# group_vars['cg_fqdn']",
         "group_vars['cg_fqdn'] = " + repr(urlparse(provider.base_url).netloc) + "\ngroup_vars['cg_fqdn']"),
        ("# group_vars['cgidp_metadata'] = 'sptestcgidp-metadata-20200918.xml'\n# group_vars['cgidp_metadata']",
         "group_vars['cgidp_metadata'] = 'ci-idp-metadata.xml'\ngroup_vars['cgidp_metadata']"),
        ("group_vars['cg_groups'] = [\n]", "group_vars['cg_groups'] = " + repr([provider.authorized_group])),
    ]
    return {find(old): (old, new) for old, new in settings}
