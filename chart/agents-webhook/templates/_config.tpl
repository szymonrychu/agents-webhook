{{/* Settings ConfigMap data; no secret values. Fails on a bad env key or a missing orchestrator url. */}}
{{- define "agents-webhook.config" -}}
WEBHOOK_ORCHESTRATOR_URL: {{ required "orchestrator.url is required" .Values.orchestrator.url | quote }}
WEBHOOK_PUBLIC_BASE_URL: {{ .Values.publicBaseUrl | quote }}
WEBHOOK_TRUSTED_PROXIES: {{ join "," (.Values.trustedProxies | default list) | quote }}
WEBHOOK_MAX_IN_PROGRESS: {{ .Values.limits.maxInProgress | int | toString | quote }}
WEBHOOK_MAX_IN_PROGRESS_PER_ENDPOINT: {{ .Values.limits.maxInProgressPerEndpoint | int | toString | quote }}
WEBHOOK_MAX_QUEUE: {{ .Values.limits.queueCap | int | toString | quote }}
WEBHOOK_MAX_BODY_BYTES: {{ .Values.limits.maxBodyBytes | int | toString | quote }}
WEBHOOK_TEMPLATE_MEMORY_MB: {{ .Values.templateMemoryMb | int | toString | quote }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "WEBHOOK_" $k) }}
{{- fail (printf "agents-webhook: env key %q must start with WEBHOOK_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}
