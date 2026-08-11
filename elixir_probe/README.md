# Elixir Probe — an interactive API prompt inside an add-on container

A remote `eval` endpoint that runs **inside an add-on container**, where the
Supervisor and Core APIs are reachable and `SUPERVISOR_TOKEN` is in scope.
Instead of writing a throwaway add-on per question, you get an Elixir prompt on
the add-on network for poking at API shapes, payloads and behaviour.

**Test bench only.** This is arbitrary remote code execution behind a shared
secret. Do not install it on a production instance, and uninstall it when done —
uninstalling revokes the token.

## Install

1. Copy this directory to the installation as `/addons/elixir_probe/`, or add
   the containing repository to the Add-on Store.
2. Install *Elixir Probe*.
3. In its **Configuration** tab set `auth_token` to something long:
   `openssl rand -hex 24`. It refuses to start without one of at least 16 chars.
4. Start it. The log should read
   `Probe listening on 4000; supervisor token present`.

## Use

```bash
export PROBE=http://<host>:4000
export TOK=<the auth_token you set>

curl -s $PROBE/health | jq

curl -s $PROBE/eval -H "x-probe-token: $TOK" --data-binary '
  core_rest(:get, "/config")
' | jq
```

`POST /eval` takes Elixir source as the raw body and answers with
`{ok, result, printed, error}`. `Probe` is imported, so its helpers are bare
calls.

### Helpers

| Call | Reaches |
|---|---|
| `token()` | this add-on's `SUPERVISOR_TOKEN` |
| `sup(:get, "/addons/self/info")` | the Supervisor API, with this add-on's own token |
| `core_rest(:get, "/config")` | Core's REST API, via the Supervisor's `/core/api/…` proxy |
| `core_ws([%{id: 1, type: "get_states"}])` | Core's WebSocket API, via the Supervisor's `/core/websocket` proxy |
| `fingerprint()` / `fingerprint_report()` | no API at all — this container from inside: env (values redacted by key), `/etc/hosts`, resolver, mounts, uid/gid, capabilities, interfaces, cgroup limits, the add-on paths. `fingerprint_report()` prefixes the version/identity header |

`core_ws/2` completes both handshakes and returns
`{:ok, %{auth: …, replies: […]}}`, one reply per command. A handshake that ends
in `auth_invalid` comes back as a result rather than an error, so you can see
what the far side said.

What each helper can actually reach depends on the permissions this add-on
declares in `config.yaml` (`hassio_api`, `hassio_role`, `homeassistant_api`) —
which is usually the thing you are trying to observe. Change them, bump
`version`, and reinstall; the Supervisor caches the manifest per version.

Prefer read-only calls. Anything that mutates state belongs on a bench you are
willing to reflash, and endpoints like `/os/datadisk/wipe`,
`/backups/*/restore` and `/network/interface/*/update` will do exactly what they
say.

## Local self-test

`test_local.exs` runs the WS client against a fake WebSocket server, including
one that pushes its first frame from `init/1` so the 101 and that frame coalesce
into a single TCP segment — a case that strands naive clients.

```bash
mix deps.get
PROBE_TOKEN=local-selftest-token-0123456789 PROBE_PORT=4321 mix run test_local.exs
# ... ALL LOCAL CHECKS PASSED
```

`PROBE_TOKEN`/`PROBE_PORT` exist only for that script; on a real install the
token comes from `/data/options.json` and the port is the one `config.yaml` maps.
