# Bench add-on repository

Home Assistant add-ons for poking at APIs from inside an add-on container, on a
**test bench**. Nothing here belongs on a production installation.

| Add-on | What it is |
|---|---|
| [`elixir_probe`](elixir_probe/) | Token-gated remote Elixir eval endpoint, for exercising the Supervisor and Core APIs interactively |
| `probe_*` | The same image, installed under different declared permissions |

## Permission variants

Which API paths an add-on may reach depends on what it declares in
`config.yaml`. The `probe_*` add-ons exist to measure that rather than assume
it: identical code, identical image, one permission set each.

| Add-on | Declares | Host port |
|---|---|---|
| `probe_none` | nothing | 4001 |
| `probe_core_api` | `homeassistant_api` | 4002 |
| `probe_default` | `hassio_api`, role `default` | 4003 |
| `probe_homeassistant` | `hassio_api`, role `homeassistant` | 4004 |
| `probe_backup` | `hassio_api`, role `backup` | 4005 |
| `probe_manager` | `hassio_api`, role `manager` | 4006 |
| `probe_admin` | `hassio_api`, role `admin` | 4007 |
| `probe_auth` | `hassio_api` + `auth_api`, role `default` | 4008 |

Each maps a distinct host port, so several can run at once. `Probe.report/1`
returns that add-on's identity as the Supervisor records it, alongside the
outcome of every path in `Probe.Matrix.paths/0` — run it on each variant and the
set of results describes the authorization surface.

Every probe is a **GET**, including against paths that only accept POST, because
authorization is decided from the path and never the method. So the matrix
reads the policy without touching state; a path that authorizes but has no GET
handler answers 405, which is itself the signal that authorization passed.

`probe_admin` holds the highest role the Supervisor grants and this image
exposes a remote eval endpoint. Bench only.

## Adding it to a HAOS installation

1. Push this directory to a GitHub repo, with `repository.yaml` at the **root**
   and each add-on in its own folder.
2. On the HAOS box: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   paste the repo URL, **Add**. If the ⋮ menu has no *Repositories* entry,
   enable **Advanced Mode** in your user profile first.
3. Install `Elixir Probe` from the store.
4. Set `auth_token` in its Configuration tab (`openssl rand -hex 24`) — it
   refuses to start without one — then Start.

The Supervisor clones the repo and **builds the image on the device** (no
`image:` key in `config.yaml`, so there is nothing to pull). First build takes a
few minutes while it fetches Hex deps.

### Layout the Supervisor expects

```
repository.yaml          # name: required. url/maintainer: optional.
elixir_probe/
  config.yaml
  Dockerfile
  ...
```

### Non-`main` branch

Append `#branch` to the URL — `https://github.com/you/repo#bench`. The
Supervisor parses it with `RE_REPOSITORY`:
`^(?P<url>[^#]+)(?:#(?P<branch>[\w\-./]+))?$`.

### Private repo

The URL is handed to GitPython as-is, so a credentialed HTTPS URL
(`https://<PAT>@github.com/you/repo`) clones fine. The PAT is then stored in the
Supervisor's config in cleartext — use a fine-grained, read-only, expiring token
if you go that way.

## Why a concrete `FROM` and no `build.yaml`

`ARG BUILD_FROM` / `FROM ${BUILD_FROM}` is the pattern most add-on docs show,
and it **fails without a `build.yaml`**. In `supervisor/apps/build.py`,
`_read_build_config` returns `None` when no build file is found ("assuming
modernized build"), `base_image` is then `None`, and `BUILD_FROM` is never added
to the build args — leaving an empty `FROM`.

So `elixir_probe/Dockerfile` pins `hexpm/elixir:1.18.4-erlang-27.3.4.16-alpine-3.21.7`
directly. That tag is multi-arch (amd64 + arm64), and it carries a current Elixir
rather than whatever the HA base image's Alpine ships.

## Alternative: no repo at all

Drop `elixir_probe/` into the installation's `/addons/` share as
`/addons/elixir_probe/` and use **⋮ → Check for updates**; it appears under
*Local add-ons*. That needs a way to write to the share — the Samba, SSH, or
Studio Code Server add-on — which is itself an install, so on a fresh box the
repo route is usually fewer steps.
