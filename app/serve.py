#!/usr/bin/env python3
"""Dev server for the dummy sheet app that disables all caching, so edits
are always visible on refresh without hard-reload gymnastics."""
import http.server
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8811
    http.server.test(HandlerClass=NoCacheHandler, port=port)
