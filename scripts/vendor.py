"""Refresh local browser assets from pinned upstream releases; commit results."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "app/static/vendor"
SOURCES = {
    "swagger-ui-bundle.js": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.31.0/dist/swagger-ui-bundle.js",
    "swagger-ui.css": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.31.0/dist/swagger-ui.css",
    "swagger-LICENSE": "https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.31.0/LICENSE",
    "bootstrap.min.css": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/dist/css/bootstrap.min.css",
    "bootstrap.bundle.min.js": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/dist/js/bootstrap.bundle.min.js",
    "bootstrap-LICENSE": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/LICENSE",
    "htmx.min.js": "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.8/dist/htmx.min.js",
    "htmx-LICENSE": "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.8/LICENSE",
}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, url in SOURCES.items():
        with urlopen(url, timeout=60) as response:
            content = response.read()
        (ROOT / name).write_bytes(content)
        manifest[name] = {"source": url, "sha256": hashlib.sha256(content).hexdigest()}
        print(f"Vendored {name}: {len(content)} bytes")
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
