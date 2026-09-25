from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import json
import logging


# Basic header info for HTTP response
BOILERPLATE = """HTTP/1.1 200 OK
Content-Type: text/json
Location: localhost:8000

"""

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Resp(BaseHTTPRequestHandler):
    # Cribbed from https://drewsh.com/minimal-python-server
    def do_GET(self):
        content = json.dumps(
            {
                "version": os.environ.get("OSL_VERSION", "unknown"),
                "deploy_date": os.environ.get("OSL_DEPLOY_DATE", "2026-10-01"),
                "tag": os.environ.get("OSL_GH_TAG", "main"),
                "env": os.environ.get("OSL_GH_env", "bb"),
            }
        )

        logger.info(f"version is {content}")

        # Write out response
        self.wfile.write((BOILERPLATE + content).encode("utf-8"))


def run(server_class=HTTPServer, handler_class=BaseHTTPRequestHandler):
    # Set up server
    service_url = os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:8111")
    parsed_url = urlparse(service_url)
    server_address = (parsed_url.hostname, parsed_url.port)
    httpd = server_class(server_address, handler_class)

    # start basic server
    httpd.serve_forever()


if __name__ == "__main__":
    logger.info("Starting server...")
    run(HTTPServer, Resp)
