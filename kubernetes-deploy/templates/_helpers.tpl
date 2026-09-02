{{/*
Expand the name of the chart.
*/}}
{{- define "bsc-tre.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "bsc-tre.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "bsc-tre.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "bsc-tre.labels" -}}
helm.sh/chart: {{ include "bsc-tre.chart" . }}
{{ include "bsc-tre.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "bsc-tre.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bsc-tre.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "bsc-tre.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "bsc-tre.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Frontend hostAliases: manual list wins; else optional auto-map domain.host -> ingress ClusterIP
so in-pod OIDC calls to http://<domain>/auth reach the ingress controller (same path as browsers).
*/}}
{{- define "openvre.frontendHostAliases" -}}
{{- if .Values.frontend.hostAliases }}
{{- toYaml .Values.frontend.hostAliases }}
{{- else if and .Values.domain.host .Values.frontend.hostAliasesAutoIngress }}
{{- $ns := .Values.frontend.ingressLookup.namespace | default "ingress-nginx" }}
{{- $svcName := .Values.frontend.ingressLookup.service | default "ingress-nginx-controller" }}
{{- $ingSvc := lookup "v1" "Service" $ns $svcName }}
{{- if and $ingSvc $ingSvc.spec.clusterIP }}
- ip: {{ $ingSvc.spec.clusterIP | quote }}
  hostnames:
    - {{ .Values.domain.host | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Optional per-project node pin for all static chart workloads.
Enable in per-namespace values (e.g. values-project-a.yaml).

Requires matching node label + taint (see isolation/base/node-pinning-example.yaml).
Scheduler reads the same values for dynamically created interactive sessions.
*/}}
{{- define "openvre.nodeSelector" -}}
{{- if and .Values.nodePinning.enabled .Values.nodePinning.nodeSelector }}
nodeSelector:
{{ toYaml .Values.nodePinning.nodeSelector | indent 2 }}
{{- end }}
{{- end }}

{{- define "openvre.tolerations" -}}
tolerations:
  - key: node.kubernetes.io/not-ready
    operator: Exists
    effect: NoExecute
    tolerationSeconds: {{ int (.Values.nodeEviction.tolerationSeconds | default 60) }}
  - key: node.kubernetes.io/unreachable
    operator: Exists
    effect: NoExecute
    tolerationSeconds: {{ int (.Values.nodeEviction.tolerationSeconds | default 60) }}
{{- if and .Values.nodePinning.enabled .Values.nodePinning.tolerations }}
{{ toYaml .Values.nodePinning.tolerations | indent 2 }}
{{- end }}
{{- end }}

{{- define "openvre.schedulerNodeSelectorEnv" -}}
{{- if and .Values.nodePinning.enabled .Values.nodePinning.nodeSelector }}
{{- $parts := list -}}
{{- range $k, $v := .Values.nodePinning.nodeSelector }}
{{- $parts = append $parts (printf "%s=%s" $k $v) -}}
{{- end }}
{{- join "," $parts -}}
{{- end }}
{{- end }}

{{- define "openvre.schedulerTolerationsEnv" -}}
{{- if and .Values.nodePinning.enabled .Values.nodePinning.tolerations }}
{{- $parts := list -}}
{{- range .Values.nodePinning.tolerations }}
{{- if eq (.operator | default "Equal") "Equal" }}
{{- $parts = append $parts (printf "%s=%s:%s" .key (.value | default "") (.effect | default "NoSchedule")) -}}
{{- else }}
{{- $parts = append $parts (printf "%s:%s" .key (.effect | default "NoSchedule")) -}}
{{- end }}
{{- end }}
{{- join "," $parts -}}
{{- end }}
{{- end }}

{{/*
Pack all chart pods onto one node in the dedicated/shared pool.
preferredHostname = home node while it is Ready; if it fails, nodeSelector
still allows the other labelled worker. Soft podAffinity keeps later pods
with the ones already running (including after failover).
Do not use a hard hostname nodeSelector — that blocks failover.
*/}}
{{- define "openvre.affinity" -}}
{{- $np := .Values.nodePinning | default dict -}}
{{- $host := $np.preferredHostname | default "" -}}
{{- $colocate := $np.colocate | default false -}}
{{- if and $np.enabled (or $host $colocate) }}
affinity:
{{- if $host }}
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - {{ $host | quote }}
{{- end }}
{{- if $colocate }}
  podAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              openvre.colocate: "true"
          topologyKey: kubernetes.io/hostname
{{- end }}
{{- end }}
{{- end }}
