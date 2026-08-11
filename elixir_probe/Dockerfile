# A concrete FROM, not `ARG BUILD_FROM` / `FROM ${BUILD_FROM}`.
#
# The Supervisor only passes BUILD_FROM when the add-on ships a build.yaml:
# `AppBuild._read_build_config` returns None when no build file is found
# ("assuming modernized build"), `base_image` is then None, and the arg is never
# added. An `ARG BUILD_FROM` Dockerfile with no build.yaml fails with an empty
# FROM. Pinning the image here is the modern path and skips the arch mapping
# entirely — this tag is multi-arch (amd64 + arm64), which covers a Green.
#
# Bonus: a current Elixir, rather than whatever the HA base image's Alpine
# happens to carry.
FROM hexpm/elixir:1.18.4-erlang-27.3.4.16-alpine-3.21.7

ENV MIX_ENV=prod
WORKDIR /app

COPY mix.exs mix.lock /app/
RUN mix local.hex --force && mix local.rebar --force && mix deps.get && mix deps.compile

COPY lib /app/lib
RUN mix compile

CMD [ "mix", "run", "--no-halt" ]
