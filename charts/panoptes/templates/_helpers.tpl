{{/*
Panoptes chart template helpers — names, labels, the fixed SA names, the image ref.
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

{{/* The standard recommended labels stamped on every chart resource. */}}
{{- define "panoptes.labels" -}}
app.kubernetes.io/name: {{ include "panoptes.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* The collector + MCP ServiceAccount names. These are FIXED (not release-scoped) so they
     match the IRSA trust subjects the Phase-6 Terraform module pins exactly —
     `system:serviceaccount:panoptes:panoptes-collector` / `:panoptes-mcp`. The IRSA
     credential is scoped to these two SA names, so they must NOT drift from the trust
     policy (a release-prefixed name would break the OIDC `:sub` StringEquals match). */}}
{{- define "panoptes.collectorServiceAccountName" -}}panoptes-collector{{- end -}}
{{- define "panoptes.mcpServiceAccountName" -}}panoptes-mcp{{- end -}}

{{/* The fully-qualified container image ref (`repository:tag`) for the collector + MCP. */}}
{{- define "panoptes.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}

{{/* The POD-level securityContext (runAsNonRoot + RuntimeDefault seccomp) from values —
     applied to every workload's pod spec. */}}
{{- define "panoptes.podSecurityContext" -}}
{{- toYaml .Values.securityContext.pod -}}
{{- end -}}

{{/* The base CONTAINER-level securityContext (drop ALL caps + no privilege escalation) from
     values. `readOnlyRootFilesystem` is added per-workload (true for the python collector/mcp;
     omitted for grafana/VM which need a writable data path). */}}
{{- define "panoptes.containerSecurityContext" -}}
{{- toYaml .Values.securityContext.container -}}
{{- end -}}
