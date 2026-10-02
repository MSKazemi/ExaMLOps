# The one-shot setup image of the Compose identity overlay (ADR 0125): a shell, openssl, and the
# SPIRE server's command-line client. The SPIRE images are distroless, so they cannot run a script.
FROM ghcr.io/spiffe/spire-server:1.15.3@sha256:4082f30d3e0ddc4000a171392c4ea174345ee44d161ee917c70b97b2ecfba141 AS spire

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
# hadolint ignore=DL3018
RUN apk add --no-cache openssl
COPY --from=spire /opt/spire/bin/spire-server /usr/local/bin/spire-server
COPY spire-init.sh /usr/local/bin/spire-init
# Root, and only for as long as it runs: it writes root-only private keys into the PKI volume.
ENTRYPOINT ["/usr/local/bin/spire-init"]
