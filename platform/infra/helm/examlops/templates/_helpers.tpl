{{/* Common naming + label helpers. */}}
{{- define "examlops.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "examlops.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "examlops.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "examlops.labels" -}}
app.kubernetes.io/name: {{ include "examlops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
examlops.io/cluster: {{ .Values.global.cluster | quote }}
examlops.io/tenant: {{ .Values.global.tenant | quote }}
{{- end -}}

{{- define "examlops.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "examlops.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Per-tier Secret names. Empty service values preserve the legacy global Secret. */}}
{{- define "examlops.controlPlaneSecret" -}}
{{- default .Values.existingSecret .Values.controlPlane.existingSecret -}}
{{- end -}}

{{- define "examlops.dashboardSecret" -}}
{{- default .Values.existingSecret .Values.dashboard.existingSecret -}}
{{- end -}}

{{- define "examlops.agentSecret" -}}
{{- default .Values.existingSecret .Values.agent.existingSecret -}}
{{- end -}}

{{/* Image ref: global.imageRegistry + repository + (tag|AppVersion).

     The registry is REQUIRED, and that is a deliberate change from a silent default.
     `global.imageRegistry: ""` composed refs like `examlops-agent:0.48.0` — an unqualified
     name, which Kubernetes resolves to `docker.io/library/examlops-agent`. The `library/`
     namespace holds Docker Official Images and nobody outside Docker can publish there, so
     the chart's own defaults produced a reference that can never be satisfied, by us or by
     anyone. It rendered and linted clean the whole time; the failure only appears in a
     cluster, as ImagePullBackOff. Failing here says the same thing at the only point where
     it is still cheap to fix. */}}
{{- define "examlops.image" -}}
{{- $reg := .root.Values.global.imageRegistry -}}
{{- if not $reg -}}
{{- fail "global.imageRegistry is required — without it image refs resolve to docker.io/library/, which only Docker can publish to. Set it to the registry holding the ExaMLOps images, e.g. --set global.imageRegistry=ghcr.io/<owner>/ (note the trailing slash)." -}}
{{- end -}}
{{- $tag := default .root.Chart.AppVersion .svc.image.tag -}}
{{- printf "%s%s:%s" $reg .svc.image.repository $tag -}}
{{- end -}}

{{/* Shared pod security + spread, injected per tier. */}}
{{- define "examlops.podSecurityContext" -}}
securityContext:
{{ toYaml .Values.podSecurityContext | indent 2 }}
{{- end -}}

{{/* Workload identity (ADR 0125). The one place a tier's SPIFFE ID is written: the ClusterSPIFFEID
     that registers it and the control plane's map of who may do what are both built from here, so
     they cannot name different IDs. Namespace and release keep two installs in one cluster apart. */}}
{{- define "examlops.spiffeID" -}}
{{- printf "spiffe://%s/ns/%s/%s/%s" .root.Values.workloadIdentity.trustDomain .root.Release.Namespace (include "examlops.fullname" .root) .component -}}
{{- end -}}

{{/* The tiers that call the control plane and are deployed by this release, in a stable order. */}}
{{- define "examlops.spiffeCallers" -}}
{{- $callers := list "dashboard" -}}
{{- if .Values.agent.enabled }}{{ $callers = append $callers "agent" }}{{ end -}}
{{- if ((.Values.events | default dict).followers | default dict).autopilot | default dict | dig "enabled" false }}{{ $callers = append $callers "autopilot-follower" }}{{ end -}}
{{- toJson $callers -}}
{{- end -}}

{{/* SPIFFE ID → principal, tenant and scopes, for the control plane. */}}
{{- define "examlops.workloadIdentityMap" -}}
{{- $root := . -}}
{{- $map := dict -}}
{{- range $component := include "examlops.spiffeCallers" $root | fromJsonArray -}}
{{- $caller := index $root.Values.workloadIdentity.callers $component -}}
{{- $_ := set $map (include "examlops.spiffeID" (dict "root" $root "component" $component)) (dict "principal" $caller.principal "tenant" $root.Values.global.tenant "scopes" $caller.scopes) -}}
{{- end -}}
{{- toJson $map -}}
{{- end -}}

{{/* The spiffe-helper sidecar: keeps the tier's JWT-SVID (or, for the control plane, the trust
     bundle) in the spiffe-svid memory volume, rewritten before it expires. */}}
{{- define "examlops.spiffeHelperContainer" -}}
{{- $h := .root.Values.workloadIdentity.helper -}}
- name: spiffe-helper
  image: {{ printf "%s:%s" $h.image.repository $h.image.tag }}{{ with $h.image.digest }}@{{ . }}{{ end }}
  imagePullPolicy: {{ $h.image.pullPolicy }}
  args: ["-config", "/etc/spiffe-helper/{{ .component }}.conf"]
  securityContext:
    {{- toYaml .root.Values.containerSecurityContext | nindent 4 }}
  resources:
    {{- toYaml $h.resources | nindent 4 }}
  volumeMounts:
    - {name: spiffe-workload-api, mountPath: /spiffe-workload-api, readOnly: true}
    - {name: spiffe-svid, mountPath: /run/spire/svid}
    - {name: spiffe-helper-config, mountPath: /etc/spiffe-helper, readOnly: true}
{{- end -}}

{{/* The volumes the helper needs. The Workload API comes through the SPIFFE CSI driver, not a
     hostPath; the SVID never touches disk. */}}
{{- define "examlops.spiffeVolumes" -}}
- name: spiffe-workload-api
  csi: {driver: {{ .Values.workloadIdentity.csiDriver }}, readOnly: true}
- name: spiffe-svid
  emptyDir: {medium: Memory, sizeLimit: 1Mi}
- name: spiffe-helper-config
  configMap: {name: {{ include "examlops.fullname" . }}-spiffe-helper}
{{- end -}}
