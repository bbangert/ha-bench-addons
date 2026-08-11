defmodule Probe.Matrix do
  @moduledoc """
  Which Supervisor paths this add-on's credential may reach, as a flat map of
  `path => outcome`.

  Read-only by construction: **every probe is a GET**, including against paths
  that only accept POST or DELETE. That is deliberate and it costs nothing,
  because the Supervisor decides authorization from the request path — the role
  patterns never consider the method — so a GET learns the same thing a POST
  would without any chance of changing state. A path that authorizes but has no
  GET handler answers 405, which is itself the signal that authorization passed.

  Each result records the raw status, a class, and — where the answer carried
  one — the API's own error message.

  The class captures what the answer's *shape* reveals:

    * `:rejected`  — the API's own error envelope
                     (`{"result": "error", "message": …}`). Something ran and
                     declined for a stated reason, which the `message` names.
    * `:refused`   — a bare `401`/`403` with no envelope.
    * `:allowed`   — 2xx.
    * `:method`    — 405: authorized, wrong verb for a read.
    * `:not_found` — 404.
    * `:other`     — anything else, with the status kept verbatim.

  `:rejected` versus `:refused` is worth having: `/core/info` and `/auth` both
  answer 403 to an add-on holding neither permission, but only the second names
  a rule (`"Can't use Home Assistant auth!"`), and flattening them into one
  bucket would let two implementations appear to agree for different reasons.

  **`:refused` does not identify which layer refused.** A bare 401/403 from the
  API layer and a bare 401/403 from a handler are indistinguishable in the
  response, and this is not hypothetical: granting `auth_api` moves `/auth` from
  `:rejected` to `:refused`, because the handler then runs, finds no credentials
  on a GET, and answers a bare 401. A calibration request with a deliberately
  invalid token does not disambiguate it — that produces the same bare 401. The
  raw status is always retained, so nothing is lost; just do not read `:refused`
  as "never reached a handler".

  Paths are baked in rather than passed by the caller so two runs against
  different systems are comparable by construction. `paths/0` is public so a
  fixture can record exactly what was asked.
  """

  # One representative path per authorization family. `_slug` stands where a
  # real add-on slug would go: it need not exist, since a path that authorizes
  # answers 404 and a path that does not answers 403, and telling those apart is
  # the entire point.
  @paths [
    # unauthenticated / bypass families
    "/info",
    "/addons/self/info",
    "/addons/self/options",
    "/addons/self/options/config",
    "/addons/self/logs",
    "/addons/self/stats",
    "/services",
    "/services/mqtt",
    "/discovery",
    "/auth",

    # core / homeassistant family
    "/core/info",
    "/core/stats",
    "/core/logs",
    "/homeassistant/info",

    # backup family
    "/backups",
    "/backups/info",

    # manager family
    "/addons",
    "/audio/info",
    "/cli/info",
    "/dns/info",
    "/docker/info",
    "/hardware/info",
    "/host/info",
    "/jobs/info",
    "/multicast/info",
    "/network/info",
    "/observer/info",
    "/os/info",
    "/resolution/info",
    "/security/info",
    "/store",
    "/store/addons",
    "/supervisor/info",
    "/supervisor/stats",
    "/supervisor/logs",
    "/mounts",
    "/available_updates",
    "/refresh_updates",
    "/auth/cache",

    # admin-only families
    "/addons/_slug/security",
    "/os/datadisk/wipe",
    "/ingress/panels",

    # supervisor-only in some implementations
    "/os/datadisk/list",
    "/host/disks/default/usage",
    "/store/addons/_slug/changelog",
    "/store/addons/_slug/documentation",

    # another add-on's namespace — reachable at all?
    "/addons/_slug/info",
    "/addons/_slug/options",
    "/addons/_slug/logs",

    # The proxies into Core. Without these the matrix cannot see
    # `homeassistant_api` at all, since that permission governs nothing on the
    # Supervisor's own surface. `/core/api/stream` is deliberately absent: a GET
    # on a streaming endpoint blocks until the receive timeout.
    "/core/api/",
    "/core/api/config",
    "/core/api/states",
    "/homeassistant/api/config",
    "/core/websocket"
  ]

  @doc "The exact path list probed, so a fixture can record it."
  @spec paths() :: [String.t()]
  def paths, do: @paths

  @doc """
  Probe every path and record status, class, and any API error message.

      matrix()
      matrix(paths: ["/info", "/addons"])
  """
  @spec matrix(keyword()) :: %{String.t() => map()}
  def matrix(opts \\ []) do
    opts
    |> Keyword.get(:paths, @paths)
    |> Map.new(fn path -> {path, outcome(Probe.sup(:get, path))} end)
  end

  @doc """
  `matrix/1` plus the context needed to compare two runs: who we were, and what
  we asked.
  """
  @spec report(keyword()) :: map()
  def report(opts \\ []) do
    %{
      versions: Probe.Header.versions(),
      identity: Probe.Header.identity(),
      paths: paths(),
      matrix: matrix(opts)
    }
  end

  # The API error envelope is `{"result": "error", "message": …}`. Its presence
  # says a stated reason came back; its absence says only that one did not, NOT
  # which layer answered (see the moduledoc's `:refused` caveat). The message is
  # the valuable part — "No access to mqtt service!", "App _slug does not exist"
  # name the rule that fired, and that is what is worth comparing across
  # implementations.
  defp outcome(%{status: status, body: body}) do
    message = api_message(body)

    class =
      cond do
        status in 200..299 -> :allowed
        status == 405 -> :method
        status == 404 and is_nil(message) -> :not_found
        status in [401, 403] and message -> :rejected
        status in [401, 403] -> :refused
        message -> :rejected
        true -> :other
      end

    %{status: status, class: class}
    |> then(fn result -> if message, do: Map.put(result, :message, message), else: result end)
  end

  defp api_message(%{"result" => "error", "message" => message}) when is_binary(message) do
    String.slice(message, 0, 200)
  end

  defp api_message(_body), do: nil
end
