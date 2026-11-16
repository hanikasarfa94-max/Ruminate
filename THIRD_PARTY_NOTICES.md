# Third-party notices

Ruminate's own source, documentation and self-authored test fixtures are licensed
under [Apache License 2.0](LICENSE). Dependencies retain their original copyrights
and licenses. Imported research documents are not relicensed by Ruminate.

## Runtime dependencies

The following direct dependencies and native parsing components were checked
against the installed distribution metadata and included license files on
2026-11-16. Exact dependency resolution is recorded in `uv.lock`.

| Component | Locked version | License / upstream |
| --- | --- | --- |
| MCP Python SDK | 2.3.0 | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE) |
| pypdf | 7.0.1 | [BSD-3-Clause](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| Trafilatura | 2.3.1 | [Apache-2.0](https://github.com/adbar/trafilatura/blob/master/LICENSE) |
| Pillow | 13.0.0 | [MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| pypdfium2 / PDFium | 5.15.2 | [Apache-2.0, BSD-3-Clause and bundled dependency licenses](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing) |
| lxml (transitive) | 6.1.3 | [BSD-3-Clause and included third-party notices](https://github.com/lxml/lxml/blob/master/LICENSES.txt) |

Ruminate's wheel declares dependencies; it does not vendor their implementations
or native binaries. Installation retrieves the original distributions with their
license files. Transitive dependencies are listed in the lockfile, and their
installed `.dist-info` metadata and license files remain authoritative.

If packaging a standalone application or copying dependencies into a bundle,
retain the complete notices from those exact distributions. In particular,
pypdfium2 includes PDFium `LICENSES/` and `BUILD_LICENSES/` material, while lxml and
Pillow wheels can include native library notices. A top-level package license is
not a substitute for those bundled notices.

## Project assets and examples

The `r.` brand artwork and synthetic fixtures were created for Ruminate. Public
repository examples do not include downloaded research papers, private notes,
host conversation archives or user databases. Referenced product names and links
remain the property of their respective owners.
