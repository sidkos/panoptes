{{/*
Panoptes chart template helpers.

Phase-0 SKELETON: the standard name + fullname + selector-label helpers the placeholder
ServiceAccount uses. Phase 7 reuses these across the workload templates.
*/}}

{{/* The base name, overridable via .Values.nameOverride. Truncated to the 63-char
     Kubernetes name limit. */}}
{{- define "panoptes.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The fully-qualified release-scoped name (release + chart name), overridable via
     .Values.fullnameOverride. Truncated to 63 chars. */}}
{{- define "panoptes.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The service-account name: an explicit .Values.serviceAccount.name wins, otherwise
     the fullname. */}}
{{- define "panoptes.serviceAccountName" -}}
{{- default (include "panoptes.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{/* The standard recommended labels stamped on every chart resource. */}}
{{- define "panoptes.labels" -}}
app.kubernetes.io/name: {{ include "panoptes.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}
