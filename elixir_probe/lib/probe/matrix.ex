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

  Outcomes are therefore coarse on purpose:

    * `:denied`    — 401/403. The credential may not reach this path.
    * `:allowed`   — 2xx. Reached the handler and it answered.
    * `:method`    — 405. Authorization passed; wrong verb for a read.
    * `:not_found` — 404. Reached routing, nothing registered.
    * `{:other, status}` — anything else, kept verbatim rather than bucketed.

  The distinction that matters for comparing two implementations is
  `:denied` versus everything else. The rest is detail that makes a diff
  readable when they disagree.

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
  Probe every path and classify the outcome.

      matrix()
      matrix(paths: ["/info", "/addons"])
  """
  @spec matrix(keyword()) :: %{String.t() => atom() | {atom(), integer()}}
  def matrix(opts \\ []) do
    opts
    |> Keyword.get(:paths, @paths)
    |> Map.new(fn path -> {path, classify(Probe.sup(:get, path).status)} end)
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

    %{identity: self_info, paths: paths(), matrix: matrix(opts)}
  end

  defp classify(status) do
    cond do
      status in [401, 403] -> :denied
      status in 200..299 -> :allowed
      status == 405 -> :method
      status == 404 -> :not_found
      true -> {:other, status}
    end
  end
end
