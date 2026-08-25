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
