"""
Proves render_page() actually solves the problem it's meant to solve: a page
whose real content is injected by JavaScript after load (exactly what DDC,
EDC, and SCA's own sites do) is invisible to requests.get() but visible
after Playwright renders it.

Uses a local HTTP server serving synthetic markup, since this sandbox has no
network access to the real DDC/EDC/SCA domains -- this is a fair test of the
mechanism (does rendering-before-parsing work at all), just not a live
end-to-end test of the real sites' exact DOM structure.
"""
import http.server
import threading
import time
import requests
import monitor as m

JS_RENDERED_PAGE = b"""
<html><body>
<div id="app">Loading...</div>
<script>
fetch('/api/bids').then(r => r.json()).then(data => {
    let html = '<table><tr><th>Bid</th><th>Title</th></tr>';
    data.forEach(row => { html += `<tr><td>${row.id}</td><td>${row.title}</td></tr>`; });
    document.getElementById('app').innerHTML = html;
});
</script>
</body></html>
"""

BIDS_JSON = b'[{"id": 1, "title": "PS 123 Roof Replacement Bid #245001"}, {"id": 2, "title": "IS 45 HVAC Upgrade Bid #245002"}]'


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/bids":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(BIDS_JSON)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(JS_RENDERED_PAGE)

    def log_message(self, *args):
        pass  # quiet


def run_server():
    server = http.server.HTTPServer(("127.0.0.1", 8934), Handler)
    server.serve_forever()


def main():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(0.5)
    url = "http://127.0.0.1:8934/"

    # Step 1: confirm a plain HTTP fetch (the OLD approach) does NOT see the
    # JS-injected content -- this is the exact failure mode DDC/EDC/SCA hit.
    plain = requests.get(url).text
    assert "Bid #245001" not in plain, "test setup broken: plain fetch shouldn't see JS content"
    print("Plain requests.get() correctly does NOT see JS-injected content (as expected).")

    # Step 2: confirm render_page() DOES see it after Chromium executes the JS.
    rendered = m.render_page(url, wait_selector="table", timeout_ms=10000)
    assert "Bid #245001" in rendered, "render_page() failed to pick up JS-injected content"
    assert "Bid #245002" in rendered
    print("render_page() DOES see JS-injected content after rendering. Fix confirmed.")


if __name__ == "__main__":
    main()
