{{- define "agents-webhook.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-webhook.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "agents-webhook.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-webhook.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agents-webhook.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agents-webhook.labels" -}}
helm.sh/chart: {{ include "agents-webhook.chart" . }}
{{ include "agents-webhook.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Call with (dict "context" . "component" "api"). */}}
{{- define "agents-webhook.componentLabels" -}}
{{ include "agents-webhook.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* imagePullSecrets from values plus global, deduplicated by name. */}}
{{- define "agents-webhook.imagePullSecrets" -}}
{{- $g := ((.Values.global | default dict).imagePullSecrets) | default list }}
{{- $seen := dict }}
{{- $all := list }}
{{- range concat (.Values.imagePullSecrets | default list) $g }}
{{- if not (hasKey $seen .name) }}
{{- $_ := set $seen .name true }}
{{- $all = append $all . }}
{{- end }}
{{- end }}
{{- if $all }}
imagePullSecrets:
  {{- toYaml $all | nindent 2 }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding the database URL; fails when no source is set. */}}
{{- define "agents-webhook.databaseSecretName" -}}
{{- $p := .Values.postgres -}}
{{- if and $p.url $p.existingSecret -}}
{{- fail "agents-webhook: set only one of postgres.url and postgres.existingSecret" -}}
{{- else if $p.existingSecret -}}
{{- $p.existingSecret -}}
{{- else if $p.url -}}
{{- include "agents-webhook.fullname" . }}-database
{{- else -}}
{{- fail "agents-webhook: a Postgres connection is required: set postgres.url or postgres.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-webhook.databaseSecretKey" -}}
{{- if .Values.postgres.existingSecret -}}
{{- .Values.postgres.existingSecretKey | default "uri" -}}
{{- else -}}
uri
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the orchestrator token; fails when none is set. */}}
{{- define "agents-webhook.tokenSecretName" -}}
{{- $o := .Values.orchestrator -}}
{{- if and $o.tokenSecret $o.tokenSecret.name -}}
{{- $o.tokenSecret.name -}}
{{- else if $o.token -}}
{{- include "agents-webhook.fullname" . }}-orchestrator
{{- else -}}
{{- fail "agents-webhook: the orchestrator token is required: set orchestrator.token or orchestrator.tokenSecret.name" -}}
{{- end -}}
{{- end }}

{{- define "agents-webhook.tokenSecretKey" -}}
{{- if and .Values.orchestrator.tokenSecret .Values.orchestrator.tokenSecret.name -}}
{{- .Values.orchestrator.tokenSecret.key | default "token" -}}
{{- else -}}
token
{{- end -}}
{{- end }}

{{/* Mount point of the admin tokens file (the Secret key is projected to "tokens"). */}}
{{- define "agents-webhook.adminTokensDir" -}}/var/run/secrets/admin{{- end }}

{{/* Name of the admin tokens Secret; fails when the admin listener is on without one. */}}
{{- define "agents-webhook.adminTokensSecret" -}}
{{- required "admin.tokensSecret.name is required when admin.enabled" .Values.admin.tokensSecret.name -}}
{{- end }}

{{/*
Container env for a component. Call with (dict "context" . "component" "api"):
the settings ConfigMap, the database URL, and the orchestrator token for api and worker only.
*/}}
{{- define "agents-webhook.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "agents-webhook.fullname" .context }}-settings
env:
  - name: WEBHOOK_DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-webhook.databaseSecretName" .context }}
        key: {{ include "agents-webhook.databaseSecretKey" .context }}
{{- if eq .component "api" }}
  - name: WEBHOOK_API_ALLOWED_CIDRS
    value: {{ join "," (.context.Values.api.allowedCidrs | default (list "127.0.0.1/32")) | quote }}
{{- if .context.Values.admin.enabled }}
  - name: WEBHOOK_ADMIN_PORT
    value: {{ .context.Values.admin.port | int | toString | quote }}
  - name: WEBHOOK_ADMIN_TOKENS_FILE
    value: {{ printf "%s/tokens" (include "agents-webhook.adminTokensDir" .context) | quote }}
{{- end }}
{{- end }}
{{- if has .component (list "api" "worker") }}
  - name: WEBHOOK_ORCHESTRATOR_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-webhook.tokenSecretName" .context }}
        key: {{ include "agents-webhook.tokenSecretKey" .context }}
{{- end }}
{{- end }}
