{{- define "products-observability-demo.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "products-observability-demo.labels" -}}
app.kubernetes.io/name: {{ include "products-observability-demo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: pochini
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
