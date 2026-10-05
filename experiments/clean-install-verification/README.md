# Clean-install verification (Docker)

Proves the "clean Linux install" acceptance: a minimal Ubuntu 24.04
container with **no source checkout** installs the release wheel via the
public install-documentation command, `qing --version` matches the
release version, the gateway serves one local synthetic first request,
and uninstalling removes the CLI while preserving the state-dir user
data.

Verified 2026-10-05 with Docker 28.4.0 on the local machine (sandbox:
`experiments/clean-install-verification/`).

## Environment details

The local Docker daemon's configured registry mirrors
(`docker.mirrors.ustc.edu.cn`, `docker.xpg666.xyz`) do not serve image
manifests, so `FROM ubuntu:24.04` cannot resolve in this environment.
The reproduced base image instead starts from the official Ubuntu
`ubuntu-base` rootfs, imported without Docker Hub:

```
curl -L -o ubuntu-base-24.04.3-base-amd64.tar.gz \
  https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.3-base-amd64.tar.gz
docker import ubuntu-base-24.04.3-base-amd64.tar.gz qingniao-ubuntu-base:2404
```

The Dockerfile here declares `FROM qingniao-ubuntu-base:2404` (official
minimal Ubuntu 24.04.3 rootfs; `docker import` reports its sha256 as
`f47726b78541...`). A machine with working Docker Hub mirrors can use a
plain `FROM ubuntu:24.04` instead — the acceptance steps are identical.

## Reproduce

From the repository (build the wheel first):

```
uv build --wheel
docker build -t qingniao-install-test \
  -f experiments/clean-install-verification/Dockerfile experiments/clean-install-verification \
  --build-arg WHEEL=dist/qingniao-0.1.0-py3-none-any.whl
```

The Dockerfile copies `qingniao.whl` from the build context, so copy the
wheel into the context directory as `qingniao.whl` (or adjust the COPY).

`install_check.py` is copied into the image and runs as the container
command using the wheel's own virtualenv interpreter. It verifies:

1. **version_consistent** — `qing --version` prints `0.1.0`.
2. **first_local_request** — `qing serve` runs against a local synthetic
   upstream (a fixture HTTP server inside the container); the applied
   config routes one request whose upstream model string is observed by
   the fixture, proving the local flow with a fresh install. No real
   provider is contacted; no real request budget is spent.
3. **uninstall_preserves_user_data** — `uv tool uninstall qingniao`
   removes the CLI (`qing` no longer resolves), while the explicit state
   directory (`/root/qing-state`: `config.json`, `serve.log`,
   `gateway.lock`, ...) is preserved, matching the documented policy that
   uninstalling keeps user data and removal is a separate manual step.
