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

  The class distinguishes **where** a refusal came from, which a status code
  alone does not:

    * `:refused`   — refused before the handler: a bare `401`/`403` whose body is
                     plain text, i.e. the API layer's own exception.
    * `:rejected`  — the handler ran and declined, recognisable by the JSON error
                     envelope (`{"result": "error", "message": …}`). Authorization
                     *passed*; the endpoint applied a rule of its own, such as an
                     add-on not declaring the service it asked about.
    * `:allowed`   — 2xx.
    * `:method`    — 405: authorized, wrong verb for a read.
    * `:not_found` — 404.
    * `:other`     — anything else, with the status kept verbatim.

  That split is the point. `/core/info` and `/auth` both answer 403 to an add-on
  holding neither permission, but the first never reached a handler and the
  second reached one that consulted `auth_api` — two different mechanisms that a
  single `:denied` bucket would flatten into a false match between two
  implementations.

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
    "/addons/_slug/logs"
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
    self_info =
      case Probe.sup(:get, "/addons/self/info") do
        %{status: 200, body: %{"data" => data}} ->
          Map.take(data, [
            "slug",
            "version",
            "hassio_api",
            "hassio_role",
            "homeassistant_api",
            "auth_api"
          ])

        other ->
          %{"error" => "could not read self info", "status" => other.status}
      end

    %{versions: versions(), identity: self_info, paths: paths(), matrix: matrix(opts)}
  end

  # `/info` is reachable by every permission set, which is what makes it usable
  # as a fixture header: a row records the system it came from regardless of how
  # little the probe was granted.
  defp versions do
    case Probe.sup(:get, "/info") do
      %{status: 200, body: %{"data" => data}} ->
        Map.take(data, ["supervisor", "homeassistant", "hassos", "arch", "machine", "channel"])

      other ->
        %{"error" => "could not read /info", "status" => other.status}
    end
  end

  # The API's own error envelope is `{"result": "error", "message": …}`; the
  # layer above it raises framework exceptions whose body is plain text. So the
  # body's *shape* is what says whether a handler ran, and the message is worth
  # keeping — "No access to mqtt service!" and "Can't use Home Assistant auth!"
  # name the rule that fired, which is the thing worth comparing.
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
