defmodule Probe.Application do
  @moduledoc """
  Boots the token-gated eval endpoint.

  Refuses to start without an `auth_token` in the add-on options. This is
  arbitrary remote code execution on the add-on network with a live
  `SUPERVISOR_TOKEN` in scope — an unauthenticated default would be a worse
  hole than the one it exists to measure.
  """

  use Application

  require Logger

  @impl true
  def start(_type, _args) do
    Application.put_env(:probe, :token, auth_token())
    port = port()

    children = [
      {Bandit, plug: Probe.Server, scheme: :http, port: port, ip: {0, 0, 0, 0}}
    ]

    Logger.info("Probe listening on #{port}; supervisor token #{token_state()}")
    Supervisor.start_link(children, strategy: :one_for_one, name: Probe.Supervisor)
  end

  # Options file on a real install; PROBE_TOKEN only exists so the local
  # self-test can boot the app outside an add-on container.
  defp auth_token do
    token =
      case File.read("/data/options.json") do
        {:ok, body} -> body |> Jason.decode!() |> Map.get("auth_token", "")
        {:error, _no_options_file} -> System.get_env("PROBE_TOKEN", "")
      end

    if String.length(token) < 16 do
      raise """
      Set `auth_token` in this add-on's configuration to at least 16 characters.

      Generate one with: openssl rand -hex 24
      """
    end

    token
  end

  # 4000 matches the `ports:` mapping in config.yaml; PROBE_PORT exists so the
  # local self-test does not fight whatever already owns 4000 on a dev box.
  defp port do
    "PROBE_PORT" |> System.get_env("4000") |> String.to_integer()
  end

  defp token_state do
    if System.get_env("SUPERVISOR_TOKEN"), do: "present", else: "MISSING"
  end
end
