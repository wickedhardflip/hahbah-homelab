# Caddy with the Porkbun DNS plugin, so it can get a wildcard cert for *.hahbah.com
# via DNS-01 (no inbound ports needed; the names point at LAN addresses).
# Pinned (this container holds the Porkbun keys and the TLS key): bump the version + digests deliberately.
FROM caddy:2.11.4-builder@sha256:369218c81ca6d6af249981221b3a5c764d886dd5b058f51d144066de13f2418d AS build
RUN xcaddy build v2.11.4 --with github.com/caddy-dns/porkbun@v0.3.1

FROM caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52
COPY --from=build /usr/bin/caddy /usr/bin/caddy
