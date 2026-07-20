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

{{/* Image ref: global.imageRegistry + repository + (tag|AppVersion). */}}
{{- define "examlops.image" -}}
{{- $reg := .root.Values.global.imageRegistry -}}
{{- $tag := default .root.Chart.AppVersion .svc.image.tag -}}
{{- printf "%s%s:%s" $reg .svc.image.repository $tag -}}
{{- end -}}

{{/* Shared pod security + spread, injected per tier. */}}
{{- define "examlops.podSecurityContext" -}}
securityContext:
{{ toYaml .Values.podSecurityContext | indent 2 }}
{{- end -}}
