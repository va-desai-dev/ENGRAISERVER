# Public repository preparation

Canonical repository: `https://github.com/va-desai-dev/ENGRAISERVER`.
The CLI/distribution remains `engrai-server`; do not rename Python imports,
XDG directories, or service names to match the repository spelling.

Preparing a local repository does not create a GitHub repository, publish
code, or publish runtime artifacts. Keep the UI's GitHub footer marked
Pending until the new repository is available.

## Review the initial source tree

Include application source, tests, documentation, schemas, engine locks,
third-party notices, deployment examples, and the built UI. Exclude local
credentials, `.env` files, model weights, local route/deployment profiles,
engine checkouts, runtime binaries, caches, and personal editor/agent state.
The `.gitignore` and `.dockerignore` describe those boundaries.

Before the first commit:

```bash
git status --short
git diff --cached --stat
git diff --cached --check
git ls-files
git remote -v
```

Review the staged contents as well as filenames. An empty remote list is
expected while the tree is being reviewed locally. Preserve the existing
AGPL license and third-party notices. The application copyright notice does
not replace those files.

## Validate the installation path

The product imperative is **simplify local LLM accessibility**. Publishing
source is not the same as shipping an end-user-ready app. Before advertising
a no-toolchain public installation, verify all of the following on each
supported release platform:

- A packaged installer provides `engrai-server` on PATH, without asking users
  to install uv, Python development tools, or activate an environment.
- The installed command launches from an unrelated directory and does not
  depend on keeping a source checkout.
- The app ships or acquires a compatible verified inference runtime; users
  do not need CMake or a compiler. State any unavoidable host-driver needs.
- First launch guides account creation, model selection, and client connection
  without making users edit engine flags or internal configuration files.
- Updates preserve user state, and install/update/uninstall behavior is tested.

The installer and automatic runtime-acquisition portions are not implemented
yet. Keep that limitation visible until the release artifacts and onboarding
actually satisfy it. Source-preview setup belongs in
[SOURCE-INSTALL.md](SOURCE-INSTALL.md), not the primary app launch instructions.

Developer validation remains separate:

```bash
uv sync --frozen --all-extras
uv run pytest
./scripts/build-ui.sh
uv build
```

Review test failures and skips before declaring a release qualified. Inspect
the wheel and source archive for private state and generated engine binaries.
Confirm the source archive includes both runtime scripts and source locks,
and the wheel includes the built UI and portable model schemas.

Test the quick start from a clean source copy with a temporary `ENGRAI_HOME`.
The initial gateway must offer account creation without preexisting models,
keys, or runtime receipts. Native runtime compilation and inference
qualification are separate from Python packaging and UI checks; follow
[RUNTIMES.md](RUNTIMES.md) before publishing native bundles.

## Publish only after local review

Create an empty GitHub repository named `ENGRAISERVER` under the intended
account. Do not initialize it with a separate README or license when pushing
this existing source tree. Confirm its visibility before uploading.

After reviewing and committing the local tree on `main`, configure the new
remote and push it:

```bash
git remote add origin https://github.com/va-desai-dev/ENGRAISERVER.git
git push -u origin main
```

These are publication steps, not part of local preparation. Once the remote
is live, enable the footer's GitHub link and verify the README clone URL,
package metadata, schema URLs, and container source label resolve to it.
Do not advertise downloadable wheels, images, or runtime bundles until those
artifacts are actually published.
