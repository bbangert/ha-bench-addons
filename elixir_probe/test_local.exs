# Self-test for Probe.core_ws/2 against a fake WebSocket server. Run before shipping the add-on to a bench:
#
#     mix run test_local.exs
#
# The fake pushes `auth_required` from init/1, which is what makes the 101 and
# the first frame land in one TCP segment — a coalescing case that strands naive
# clients, since the frame sits in the HTTP/1 buffer until more bytes arrive. If
# the leftover handling regresses, this hangs on the handshake instead of passing.

defmodule FakeProxy do
  @behaviour WebSock

  @impl true
  def init(_arg) do
    {:push, {:text, Jason.encode!(%{type: "auth_required", ha_version: "2026.8.1"})},
     %{authed: false}}
  end

  @impl true
  def handle_in({data, opcode: :text}, %{authed: false} = state) do
    reply =
      if Jason.decode!(data)["access_token"] == "good-token" do
        %{type: "auth_ok", ha_version: "2026.8.1"}
      else
        %{type: "auth_invalid", message: "Invalid access"}
      end

    {:push, {:text, Jason.encode!(reply)}, %{state | authed: reply.type == "auth_ok"}}
  end

  def handle_in({data, opcode: :text}, state) do
    msg = Jason.decode!(data)

    reply = %{
      id: msg["id"],
      type: "result",
      success: true,
      result: %{echoed_type: msg["type"], echoed_id: msg["id"]}
    }

    {:push, {:text, Jason.encode!(reply)}, state}
  end

  @impl true
  def handle_info(_msg, state), do: {:ok, state}

  @impl true
  def terminate(_reason, _state), do: :ok
end

defmodule FakeRouter do
  use Plug.Router

  plug(:match)
  plug(:dispatch)

  get "/core/websocket" do
    conn |> WebSockAdapter.upgrade(FakeProxy, %{}, timeout: 60_000) |> halt()
  end

  match _ do
    send_resp(conn, 404, "")
  end
end

{:ok, _pid} = Bandit.start_link(plug: FakeRouter, scheme: :http, port: 4999)
Process.sleep(200)

opts = [host: "127.0.0.1", port: 4999, token: "good-token", timeout: 5_000]

IO.puts("\n== handshake + two commands ==")

{:ok, result} =
  Probe.core_ws(
    [
      %{id: 1, type: "get_states"},
      %{id: 2, type: "get_config", extra: "payload"}
    ],
    opts
  )

IO.inspect(result, label: "result", pretty: true)

true = result.auth["type"] == "auth_ok"
[first, second] = result.replies
true = first["result"]["echoed_type"] == "get_states"
true = second["result"]["echoed_type"] == "get_config"

IO.puts("\n== a token the proxy rejects ==")
{:ok, refused} = Probe.core_ws([%{id: 1, type: "get_states"}], Keyword.put(opts, :token, "nope"))
IO.inspect(refused, label: "refused", pretty: true)
true = refused.auth["type"] == "auth_invalid"
true = refused.replies == []

IO.puts("\n== connection refused is an error tuple, not a crash ==")
IO.inspect(Probe.core_ws([], host: "127.0.0.1", port: 4998, timeout: 1_000), label: "closed port")

# --- the eval endpoint itself, against the app this script is running inside ---

port = System.get_env("PROBE_PORT", "4000")
base = "http://127.0.0.1:#{port}"
token = System.get_env("PROBE_TOKEN")
auth = [{"x-probe-token", token}]

IO.puts("\n== /health ==")
%{status: 200, body: health} = Req.get!(base <> "/health")
IO.inspect(health, label: "health")

IO.puts("\n== /eval without a token ==")
%{status: 401} = Req.post!(base <> "/eval", body: "1 + 1")
IO.puts("401 as expected")

IO.puts("\n== /eval with a bad token ==")
%{status: 401} = Req.post!(base <> "/eval", body: "1 + 1", headers: [{"x-probe-token", "wrong"}])
IO.puts("401 as expected")

IO.puts("\n== /eval evaluates, captures output, and reports raises ==")
%{status: 200, body: ok} = Req.post!(base <> "/eval", body: "x = 20\nx * 2 + 2", headers: auth)
IO.inspect(ok, label: "arithmetic")
true = ok["ok"] and ok["result"] == "42"

%{body: printed} = Req.post!(base <> "/eval", body: ~s|IO.puts("hello"); :done|, headers: auth)
IO.inspect(printed, label: "printed")
true = printed["printed"] == "hello\n"

%{body: raised} = Req.post!(base <> "/eval", body: ~s|raise "boom"|, headers: auth)
true = raised["ok"] == false and raised["error"] =~ "boom"
IO.puts("raise reported, connection survived")

%{body: helper} = Req.post!(base <> "/eval", body: "token()", headers: auth)
IO.inspect(helper["result"], label: "Probe imported (token/0 reachable)")

IO.puts("\nALL LOCAL CHECKS PASSED")
