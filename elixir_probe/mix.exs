defmodule Probe.MixProject do
  use Mix.Project

  def project do
    [
      app: :probe,
      version: "0.1.0",
      elixir: "~> 1.15",
      deps: deps()
    ]
  end

  def application do
    [extra_applications: [:logger, :inets, :ssl], mod: {Probe.Application, []}]
  end

  defp deps do
    [
      {:bandit, "~> 1.5"},
      {:plug, "~> 1.16"},
      {:jason, "~> 1.4"},
      {:req, "~> 0.5"},
      {:mint, "~> 1.6"},
      {:mint_web_socket, "~> 1.0"},
      # Local self-test only (test_local.exs); never in the add-on image, which
      # builds with MIX_ENV=prod.
      {:websock_adapter, "~> 0.5", only: :dev}
    ]
  end
end
