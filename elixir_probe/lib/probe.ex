defmodule Probe do
  @moduledoc """
  Helpers imported into every `/eval` snippet.

  Three surfaces are reachable from inside an add-on container, all on the
  add-on network at the hostname `supervisor`:

    * `sup/3`       — the Supervisor's own API, with this add-on's token.
    * `core_rest/3` — Core's REST API, via the Supervisor's `/core/api/…` proxy.
    * `core_ws/2`   — Core's WebSocket API, via the Supervisor's
                      `/core/websocket` proxy.

  What each can reach depends on the permissions the add-on declares in
  `config.yaml`, which is usually what you are trying to observe.

  `:host`/`:port` are overridable on every call so the same code runs against a
  local fake in a test.
  """

  @supervisor_url "http://supervisor"

  @doc "This add-on's Supervisor token, from the container environment."
  @spec token() :: String.t()
  def token, do: System.get_env("SUPERVISOR_TOKEN", "")

  @doc """
  Call the Supervisor API with this add-on's own token.

      sup(:get, "/addons/self/info")
  """
  @spec sup(atom(), String.t(), keyword()) :: map()
  def sup(method, path, opts \\ []) do
    base = Keyword.get(opts, :base, System.get_env("PROBE_SUPERVISOR_URL", @supervisor_url))
    request(method, base <> path, opts)
  end

  @doc """
  Call Core's REST API through the Supervisor's `/core/api/…` proxy.

      core_rest(:get, "/config")
      core_rest(:get, "/states")
  """
  @spec core_rest(atom(), String.t(), keyword()) :: map()
  def core_rest(method, path, opts \\ []) do
    sup(method, "/core/api" <> path, opts)
  end

  defp request(method, url, opts) do
    req =
      [
        method: method,
        url: url,
        headers: [{"authorization", "Bearer " <> token()}],
        receive_timeout: Keyword.get(opts, :timeout, 15_000),
        retry: false
      ]
      |> then(fn r -> if b = opts[:json], do: Keyword.put(r, :json, b), else: r end)

    case Req.request(req) do
      {:ok, resp} -> %{status: resp.status, body: resp.body}
      {:error, reason} -> %{status: :error, body: inspect(reason)}
    end
  end

  @doc """
  Open the Supervisor's Core WebSocket proxy, complete both handshakes, send
  each command in order, and return one reply per command.

      core_ws([%{id: 1, type: "get_states"}])
      core_ws([%{id: 1, type: "get_config"}, %{id: 2, type: "get_services"}])

  Returns `{:ok, %{auth: reply, replies: [...]}}` or `{:error, reason}`. The
  handshake reply is always included, so an `auth_invalid` comes back as a
  result rather than an error and you can see what the far side said.
  """
  @spec core_ws([map()], keyword()) :: {:ok, map()} | {:error, term()}
  def core_ws(commands, opts \\ []) when is_list(commands) do
    host = Keyword.get(opts, :host, "supervisor")
    port = Keyword.get(opts, :port, 80)
    path = Keyword.get(opts, :path, "/core/websocket")
    timeout = Keyword.get(opts, :timeout, 15_000)
    auth_token = Keyword.get(opts, :token, token())

    with {:ok, conn, ref} <- connect(host, port, path),
         {:ok, conn, websocket, leftover} <- await_upgrade(conn, ref, timeout) do
      state = %{conn: conn, ref: ref, ws: websocket, timeout: timeout, frames: []}

      try do
        state |> absorb(leftover) |> converse(auth_token, commands)
      after
        Mint.HTTP.close(state.conn)
      end
    end
  end

  defp connect(host, port, path) do
    case Mint.HTTP.connect(:http, host, port, mode: :active, protocols: [:http1]) do
      {:ok, conn} ->
        case Mint.WebSocket.upgrade(:ws, conn, path, []) do
          {:ok, conn, ref} ->
            {:ok, conn, ref}

          {:error, conn, reason} ->
            Mint.HTTP.close(conn)
            {:error, {:upgrade_failed, reason}}
        end

      {:error, reason} ->
        {:error, {:connect_failed, reason}}
    end
  end

  # The 101 and the server's first frame can arrive in one TCP segment, which
  # leaves the frame sitting in Mint.HTTP1's buffer where `stream/2` will not
  # surface it until more bytes happen to arrive. So the leftover is drained
  # here and fed back in; `test_local.exs` reproduces the case deliberately.
  defp await_upgrade(conn, ref, timeout) do
    receive do
      message ->
        case Mint.WebSocket.stream(conn, message) do
          {:ok, conn, responses} ->
            status = find_response(responses, ref, :status)
            headers = find_response(responses, ref, :headers)

            if status && headers do
              {leftover, conn} = take_leftover(conn)

              case Mint.WebSocket.new(conn, ref, status, headers) do
                {:ok, conn, websocket} -> {:ok, conn, websocket, leftover}
                {:error, _conn, reason} -> {:error, {:ws_init_failed, reason}}
              end
            else
              await_upgrade(conn, ref, timeout)
            end

          {:error, _conn, reason, _responses} ->
            {:error, {:stream_failed, reason}}
        end
    after
      timeout -> {:error, :upgrade_timeout}
    end
  end

  defp find_response(responses, ref, kind) do
    Enum.find_value(responses, fn
      {^kind, ^ref, value} -> value
      _other -> nil
    end)
  end

  defp take_leftover(conn) do
    case conn do
      %{buffer: bin} when is_binary(bin) and bin != <<>> -> {bin, %{conn | buffer: <<>>}}
      _no_leftover -> {<<>>, conn}
    end
  end

  defp absorb(state, <<>>), do: state

  defp absorb(state, data) do
    case Mint.WebSocket.decode(state.ws, data) do
      {:ok, ws, frames} -> %{state | ws: ws, frames: state.frames ++ frames}
      {:error, ws, _reason} -> %{state | ws: ws}
    end
  end

  defp converse(state, auth_token, commands) do
    with {:ok, state, hello} <- recv_json(state),
         :ok <- expect(hello, "auth_required"),
         {:ok, state} <- send_json(state, %{type: "auth", access_token: auth_token}),
         {:ok, state, auth} <- recv_json(state) do
      if auth["type"] == "auth_ok" do
        case collect(state, commands, []) do
          {:ok, replies} -> {:ok, %{auth: auth, replies: replies}}
          error -> error
        end
      else
        {:ok, %{auth: auth, replies: [], note: "the proxy refused our token"}}
      end
    end
  end

  defp collect(_state, [], acc), do: {:ok, Enum.reverse(acc)}

  defp collect(state, [command | rest], acc) do
    with {:ok, state} <- send_json(state, command),
         {:ok, state, reply} <- recv_json(state) do
      collect(state, rest, [reply | acc])
    end
  end

  defp send_json(state, payload) do
    with {:ok, ws, data} <- Mint.WebSocket.encode(state.ws, {:text, Jason.encode!(payload)}),
         {:ok, conn} <- Mint.WebSocket.stream_request_body(state.conn, state.ref, data) do
      {:ok, %{state | ws: ws, conn: conn}}
    else
      {:error, _term, reason} -> {:error, {:send_failed, reason}}
    end
  end

  # Control frames are skipped rather than returned, so a heartbeat ping can
  # never be mistaken for a command reply.
  defp recv_json(%{frames: [{:text, payload} | rest]} = state) do
    {:ok, %{state | frames: rest}, Jason.decode!(payload)}
  end

  defp recv_json(%{frames: [_control | rest]} = state) do
    recv_json(%{state | frames: rest})
  end

  defp recv_json(%{frames: []} = state) do
    receive do
      message ->
        case Mint.WebSocket.stream(state.conn, message) do
          {:ok, conn, responses} ->
            data =
              responses
              |> Enum.filter(&match?({:data, ref, _} when ref == state.ref, &1))
              |> Enum.map(fn {:data, _ref, bin} -> bin end)
              |> IO.iodata_to_binary()

            recv_json(absorb(%{state | conn: conn}, data))

          {:error, _conn, reason, _responses} ->
            {:error, {:stream_failed, reason}}
        end
    after
      state.timeout -> {:error, :receive_timeout}
    end
  end

  defp expect(%{"type" => type}, type), do: :ok
  defp expect(message, expected), do: {:error, {:unexpected, expected, message}}
end
