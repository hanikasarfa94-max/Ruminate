# Release checklist

Publishing source to GitHub, creating a GitHub release and uploading a package to
PyPI are separate actions. CI validates the code and packages; it does not publish
packages or require release credentials.

1. Update the package and plugin versions together, including `uv.lock`, and
   describe user-visible changes in [CHANGELOG.md](../CHANGELOG.md).
2. Run the checks in [CONTRIBUTING.md](../CONTRIBUTING.md), the browser workflow for
   UI changes, and relevant actual host smoke tests. Record which environment ran.
3. Build with `uv build`; inspect both the wheel and source distribution. Include
   LICENSE and NOTICE, exclude local configuration, documents and generated data.
4. Run the clean-environment installation check:

   ```bash
   uv run python scripts/smoke_wheel.py dist/ruminate_local-VERSION-py3-none-any.whl
   ```

5. Review staged contents with `git diff --cached --check` and
   `uv run python scripts/check_public_tree.py`. Check the dependency notices after
   changing the lockfile, including any licenses shipped with native libraries.
6. After CI passes on the intended commit, create the release/tag and attach only
   the reviewed artifacts. Describe supported platforms and any migration steps.

Keep credentials in the publishing service's secret store. Imported source
documents, private test stores, host sessions and generated registration files
are never release artifacts.
