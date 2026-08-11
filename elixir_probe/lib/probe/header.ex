defmodule Probe.Header do
  @moduledoc """
  Who this add-on was and what system it ran on — the stamp that makes any
  report worth keeping comparable later.

  Both calls use paths an add-on reaches regardless of what it was granted:
  `/info` is api-bypass, and `/addons/self/…` is always the add-on's own. So a
  report records where it came from even when the probe holds almost no
  permissions.

  A call that fails yields `%{"error" => …, "status" => …}` in place of the
  block, because a header that could not be read is still worth carrying next
  to the body it belongs to.
  """

  @doc "The system underneath: Supervisor/Core/OS versions, arch, machine, channel."
  @spec versions() :: map()
  def versions do
    take("/info", ["supervisor", "homeassistant", "hassos", "arch", "machine", "channel"])
  end

  @doc "This add-on as the Supervisor sees it: slug, version, and the API permissions in force."
  @spec identity() :: map()
  def identity do
    take("/addons/self/info", [
      "slug",
      "version",
      "hassio_api",
      "hassio_role",
      "homeassistant_api",
      "auth_api"
    ])
  end

  defp take(path, keys) do
    case Probe.sup(:get, path) do
      %{status: 200, body: %{"data" => data}} -> Map.take(data, keys)
      other -> %{"error" => "could not read #{path}", "status" => other.status}
    end
  end
end
