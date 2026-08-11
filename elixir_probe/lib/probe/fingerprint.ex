defmodule Probe.Fingerprint do
  @moduledoc """
  What an add-on container looks like from inside it: the environment it was
  handed, the filesystem it can see, the identity it runs as, and the network
  it sits on.

  Everything is read from within the container — `/proc`, `/sys`, `:inet` —
  rather than asked of the Supervisor, so a report records what the container
  actually *got*, not what its manifest asked for. Where those two differ is
  usually the interesting part.

  Two conventions hold throughout:

    * **Nothing raises.** A path that is absent, unreadable, or in an
      unexpected shape yields `%{"error" => reason}` in place of the value it
      would have carried, at whatever depth the failure happened. The result is
      always encodable, and a partial reading is always still a reading —
      cgroup v1, for instance, has none of the v2 limit files.
    * **Environment values are redacted by key.** Any variable whose *name*
      contains `TOKEN`, `SECRET`, `PASSWORD` or `PRIVATE`, or ends in `KEY`,
      reports `"<redacted N bytes>"` in place of its value. The byte count is
      kept because whether a credential is present, and how long it is, is
      worth comparing; its value never is. The rule reads the name, not the
      value, so a secret in an unconventionally named variable comes out
      verbatim — check before publishing a capture.

  Nothing is normalised. Values that differ from run to run (hostnames, cgroup
  paths, link-local addresses) are reported as found; deciding what counts as
  noise belongs to whoever reads the report, not to the reading.
  """

  # Paths the Supervisor may map into an add-on. Fixed rather than discovered by
  # listing `/`, so a container that was given nothing and a container that was
  # given everything produce the same set of keys.
  @well_known ~w(/data /config /share /ssl /media /backup /addons /addon_configs /homeassistant)

  # The confinement lines out of /proc/self/status: what capabilities survived,
  # and whether privilege can be regained.
  @status_keys ~w(CapInh CapPrm CapEff CapBnd CapAmb NoNewPrivs Seccomp)

  # The VFS flags a mount can carry. Every one is reported for every mount,
  # present or not, so an off flag reads as `false` instead of as a key that is
  # simply not there — the two are otherwise indistinguishable to a reader.
  @mount_flags ~w(nosuid nodev noexec relatime noatime nodiratime strictatime
                  lazytime sync dirsync nosymfollow)

  @redact ~r/TOKEN|SECRET|PASSWORD|PRIVATE|KEY$/i

  @doc """
  The container as seen from inside, as a map of the sections named in the
  moduledoc.

      fingerprint()["well_known"]["/data"]
  """
  @spec fingerprint() :: map()
  def fingerprint do
    %{
      "hostname" => hostname(),
      "env" => env(),
      "etc_hosts" => etc_hosts(),
      "resolv_conf" => resolv_conf(),
      "mounts" => mounts(),
      "ids" => ids(),
      "status" => status(),
      "interfaces" => interfaces(),
      "cgroup" => cgroup(),
      "well_known" => well_known(),
      "proc" => proc()
    }
  end

  @doc "`fingerprint/0` plus the header that says which system and add-on it describes."
  @spec report() :: map()
  def report do
    %{
      versions: Probe.Header.versions(),
      identity: Probe.Header.identity(),
      fingerprint: fingerprint()
    }
  end

  defp hostname do
    case :inet.gethostname() do
      {:ok, name} -> to_string(name)
      other -> error(other)
    end
  end

  defp env do
    Map.new(System.get_env(), fn {name, value} ->
      if Regex.match?(@redact, name) do
        {name, "<redacted #{byte_size(value)} bytes>"}
      else
        {name, value}
      end
    end)
  end

  defp etc_hosts do
    with {:ok, content} <- read("/etc/hosts") do
      content
      |> lines()
      |> Enum.flat_map(fn line ->
        case String.split(String.replace(line, ~r/#.*$/, "")) do
          [ip, name | rest] -> [%{"ip" => ip, "names" => [name | rest]}]
          _blank_or_addressless -> []
        end
      end)
    end
  end

  defp resolv_conf do
    empty = %{"nameservers" => [], "search" => [], "options" => []}

    with {:ok, content} <- read("/etc/resolv.conf") do
      content
      |> lines()
      # `;` starts a comment here as well as `#`.
      |> Enum.map(&String.split(String.replace(&1, ~r/[#;].*$/, "")))
      |> Enum.reduce(empty, fn
        ["nameserver", server | _ignored], acc ->
          Map.update!(acc, "nameservers", &(&1 ++ [server]))

        ["search" | domains], acc ->
          Map.update!(acc, "search", &(&1 ++ domains))

        ["options" | options], acc ->
          Map.update!(acc, "options", &(&1 ++ options))

        _other_directive, acc ->
          acc
      end)
    end
  end

  defp mounts do
    with {:ok, content} <- read("/proc/self/mountinfo") do
      content
      |> lines()
      |> Enum.flat_map(&mount/1)
      |> Enum.sort_by(& &1["target"])
    end
  end

  # A mountinfo line splits at a lone `-`: ID, parent, device, root, target,
  # options, then any number of optional `tag:value` fields, then fstype, mount
  # source, super options. The optional fields are why the tail cannot simply be
  # indexed from the front.
  #
  # A line that does not match that shape is dropped rather than reported: the
  # format is fixed, so a mismatch would mean the kernel changed it, which is
  # not a fact about any one mount.
  defp mount(line) do
    with [head, tail] <- String.split(line, " - ", parts: 2),
         [_id, _parent, _device, root, target, options | _optional] <- String.split(head),
         [fstype, source | _super_options] <- String.split(tail) do
      set = String.split(options, ",")

      [
        %{
          "target" => target,
          # A bind mount of a subtree shows the subtree in field 4; gluing it
          # onto the source is what makes two such mounts distinguishable.
          "source" => if(root == "/", do: source, else: source <> root),
          "fstype" => fstype,
          "ro" => "ro" in set,
          "options" => options,
          "flags" => Map.new(@mount_flags, &{&1, &1 in set})
        }
      ]
    else
      _unrecognised -> []
    end
  end

  defp ids do
    with {:ok, content} <- read("/proc/self/status") do
      fields = status_fields(content)

      %{"groups" => groups(fields)}
      |> Map.merge(id_pair(fields, "Uid", "uid", "euid"))
      |> Map.merge(id_pair(fields, "Gid", "gid", "egid"))
    end
  end

  # Uid:/Gid: carry four values — real, effective, saved, filesystem. Only the
  # first two are reported; the other two never differ in a container we did not
  # ourselves manipulate.
  defp id_pair(fields, line, real_key, effective_key) do
    case fields[line] && String.split(fields[line]) do
      [real, effective | _saved_and_fs] ->
        %{real_key => integer(real), effective_key => integer(effective)}

      _absent_or_short ->
        missing = error("no #{line}: line in /proc/self/status")
        %{real_key => missing, effective_key => missing}
    end
  end

  defp groups(fields) do
    case fields["Groups"] do
      nil -> error("no Groups: line in /proc/self/status")
      value -> value |> String.split() |> Enum.map(&integer/1) |> Enum.sort()
    end
  end

  defp status do
    with {:ok, content} <- read("/proc/self/status") do
      content |> status_fields() |> Map.take(@status_keys)
    end
  end

  defp status_fields(content) do
    content
    |> lines()
    |> Map.new(fn line ->
      case String.split(line, ":", parts: 2) do
        [name, value] -> {name, String.trim(value)}
        [name] -> {name, ""}
      end
    end)
  end

  defp interfaces do
    case :inet.getifaddrs() do
      {:ok, ifaddrs} -> ifaddrs |> Enum.map(&interface/1) |> Enum.sort_by(& &1["name"])
      {:error, reason} -> error(reason)
    end
  end

  defp interface({name, opts}) do
    flags =
      opts
      |> Keyword.get_values(:flags)
      |> List.flatten()
      |> Enum.map(&to_string/1)
      |> Enum.sort()

    %{
      "name" => to_string(name),
      "flags" => flags,
      "hwaddr" => hwaddr(opts),
      "addrs" => addrs(opts)
    }
  end

  defp hwaddr(opts) do
    case Keyword.get(opts, :hwaddr) do
      bytes when is_list(bytes) -> Enum.map_join(bytes, ":", &Base.encode16(<<&1>>, case: :lower))
      _absent -> nil
    end
  end

  # `getifaddrs` returns one flat option list per interface in which each
  # `:addr` is followed by the `:netmask` belonging to it, so an interface with
  # several addresses can only be read positionally, never with `Keyword.get`.
  #
  # `prefix_or_mask` carries the mask as text for both families, since that is
  # what the runtime hands back; converting an IPv6 mask to a prefix length
  # would silently normalise a non-contiguous one into a lie.
  defp addrs(opts) do
    opts
    |> Enum.reduce([], fn
      {:addr, addr}, acc ->
        [%{"family" => family(addr), "addr" => ntoa(addr), "prefix_or_mask" => nil} | acc]

      {:netmask, mask}, [%{"prefix_or_mask" => nil} = pending | rest] ->
        [%{pending | "prefix_or_mask" => ntoa(mask)} | rest]

      _other, acc ->
        acc
    end)
    |> Enum.reverse()
  end

  defp family(address) when tuple_size(address) == 4, do: "inet"
  defp family(address) when tuple_size(address) == 8, do: "inet6"
  defp family(_address), do: "unknown"

  defp ntoa(address) do
    case :inet.ntoa(address) do
      {:error, reason} -> error(reason)
      text -> to_string(text)
    end
  end

  defp cgroup do
    %{
      "self" => cgroup_self(),
      "memory_max" => trimmed_file("/sys/fs/cgroup/memory.max"),
      "cpu_max" => trimmed_file("/sys/fs/cgroup/cpu.max"),
      "pids_max" => trimmed_file("/sys/fs/cgroup/pids.max")
    }
  end

  defp cgroup_self do
    with {:ok, content} <- read("/proc/self/cgroup"), do: lines(content)
  end

  defp well_known, do: Map.new(@well_known, fn path -> {path, path_facts(path)} end)

  defp path_facts(path) do
    case File.stat(path) do
      {:ok, stat} ->
        %{
          "exists" => true,
          "type" => to_string(stat.type),
          "mode" => mode(stat.mode),
          "uid" => stat.uid,
          "gid" => stat.gid,
          "writable" => stat.access in [:read_write, :write]
        }

      {:error, :enoent} ->
        %{"exists" => false}

      {:error, reason} ->
        error(reason)
    end
  end

  # Low 12 bits only: the type bits above them are already reported as `type`.
  defp mode(mode) do
    mode |> Bitwise.band(0o7777) |> Integer.to_string(8) |> String.pad_leading(4, "0")
  end

  defp proc do
    %{
      "pid1_comm" => trimmed_file("/proc/1/comm"),
      "oom_score_adj" => integer_file("/proc/self/oom_score_adj")
    }
  end

  defp trimmed_file(path) do
    with {:ok, content} <- read(path), do: String.trim(content)
  end

  defp integer_file(path) do
    with {:ok, content} <- read(path), do: integer(content)
  end

  # `read/1` answers the error map itself, so a caller can pass it straight
  # through an `else`-less `with` and land the error where the value belonged.
  defp read(path) do
    case File.read(path) do
      {:ok, content} -> {:ok, content}
      {:error, reason} -> error(reason)
    end
  end

  defp lines(content), do: String.split(content, "\n", trim: true)

  defp integer(text) do
    case Integer.parse(String.trim(text)) do
      {value, _rest} -> value
      :error -> error("not an integer: #{inspect(text)}")
    end
  end

  defp error(reason) when is_binary(reason), do: %{"error" => reason}
  defp error(reason) when is_atom(reason), do: %{"error" => Atom.to_string(reason)}
  defp error(reason), do: %{"error" => inspect(reason)}
end
