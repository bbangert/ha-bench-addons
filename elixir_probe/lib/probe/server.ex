defmodule Probe.Server do
  @moduledoc """
  `POST /eval` — body is Elixir source, response is JSON carrying the inspected
  result, anything the snippet printed, and any exception. `Probe` is imported,
  so `sup(:get, "/addons")` and friends work as bare calls.

  Auth is a constant-time compare on `x-probe-token`. Nothing here is built to
  survive hostile traffic; it is built to be unusable by accident.
  """

  use Plug.Router

  plug(:match)
  plug(:dispatch)

  get "/health" do
    send_json(conn, 200, %{
      ok: true,
      elixir: System.version(),
      otp: System.otp_release(),
      supervisor_token: if(System.get_env("SUPERVISOR_TOKEN"), do: "present", else: "missing")
    })
  end

  post "/eval" do
    {:ok, code, conn} = Plug.Conn.read_body(conn, length: 1_000_000)

    if authorized?(conn) do
      send_json(conn, 200, evaluate(code))
    else
      send_json(conn, 401, %{error: "bad or missing x-probe-token"})
    end
  end

  match _ do
    send_json(conn, 404, %{error: "not found"})
  end

  defp authorized?(conn) do
    expected = Application.get_env(:probe, :token, "")

    case Plug.Conn.get_req_header(conn, "x-probe-token") do
      [given] -> byte_size(given) == byte_size(expected) and :crypto.hash_equals(given, expected)
      _absent_or_repeated -> false
    end
  end

  # Group-leader swap rather than ExUnit.CaptureIO: this runs under
  # MIX_ENV=prod, where the capture server is not started.
  defp evaluate(code) do
    {:ok, io} = StringIO.open("")
    original = Process.group_leader()
    Process.group_leader(self(), io)

    result =
      try do
        {value, _binding} = Code.eval_string("import Probe\n" <> code, [], __ENV__)

        %{
          ok: true,
          result: inspect(value, pretty: true, limit: :infinity, printable_limit: 65_536)
        }
      rescue
        error -> %{ok: false, error: Exception.format(:error, error, __STACKTRACE__)}
      catch
        kind, reason -> %{ok: false, error: Exception.format(kind, reason, __STACKTRACE__)}
      after
        Process.group_leader(self(), original)
      end

    {:ok, {_input, printed}} = StringIO.close(io)
    Map.put(result, :printed, printed)
  end

  defp send_json(conn, status, payload) do
    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, Jason.encode!(payload, pretty: true))
  end
end
