# OpenBao server configuration for the opt-in `secrets` Compose profile (ADR 0011).
#
# Single node, file storage on the `openbao_data` named volume. The listener is plain HTTP because
# the port is reachable only on the internal Compose network (it is never published to the host);
# put TLS in front of it, or use the Helm chart with a service mesh, before exposing it anywhere.
# The server starts sealed and uninitialised - see docs/runbooks/openbao.md for init/unseal.
ui            = false
disable_mlock = true          # a container without IPC_LOCK; the data is encrypted at rest anyway
api_addr      = "http://openbao:8200"

storage "file" {
  path = "/openbao/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
