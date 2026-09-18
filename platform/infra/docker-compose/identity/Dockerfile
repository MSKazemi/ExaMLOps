# The one-shot setup image of the Compose identity overlay (ADR 0125): a shell, openssl, and the
# SPIRE server's command-line client. The SPIRE images are distroless, so they cannot run a script.
FROM ghcr.io/spiffe/spire-server:1.15.3@sha256:4082f30d3e0ddc4000a171392c4ea174345ee44d161ee917c70b97b2ecfba141 AS spire

FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
# hadolint ignore=DL3018
RUN apk add --no-cache openssl
COPY --from=spire /opt/spire/bin/spire-server /usr/local/bin/spire-server
COPY spire-init.sh /usr/local/bin/spire-init
# Root, and only for as long as it runs: it writes root-only private keys into the PKI volume.
ENTRYPOINT ["/usr/local/bin/spire-init"]
