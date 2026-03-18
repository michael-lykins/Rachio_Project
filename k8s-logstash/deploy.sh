#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  deploy.sh — Apply or tear down the elastic-stack namespace
#
#  Usage:
#    ./deploy.sh up       # deploy Logstash (kubectl) + Elastic Agent (Helm)
#    ./deploy.sh down     # delete everything
#    ./deploy.sh status   # show pod/service status
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

NAMESPACE="elastic-stack"
AGENT_VERSION="9.3.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Helpers ──────────────────────────────────────────────────────────────────

info()    { echo "[INFO]  $*"; }
warn()    { echo "[WARN]  $*" >&2; }
err()     { echo "[ERROR] $*" >&2; exit 1; }

require_kubectl() {
  command -v kubectl &>/dev/null || err "kubectl not found — install it or add it to PATH"
}

require_helm() {
  command -v helm &>/dev/null || err "helm not found — install it: https://helm.sh/docs/intro/install/"
}

check_logstash_secrets() {
  if grep -q "<REPLACE_WITH_YOUR_ES_API_KEY>" "${SCRIPT_DIR}/logstash/secret.yaml" 2>/dev/null; then
    warn "logstash/secret.yaml still has a placeholder for ES_API_KEY — fill it in before deploying"
    read -r -p "Continue anyway? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || err "Aborted."
  fi
}

check_agent_values() {
  if grep -q "<REPLACE_WITH_DECODED_API_KEY>" "${SCRIPT_DIR}/elastic-agent/values.yaml" 2>/dev/null; then
    warn "elastic-agent/values.yaml still has a placeholder for api_key"
    warn "Decode it with: echo '<base64_key>' | base64 -d"
    read -r -p "Continue anyway? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || err "Aborted."
  fi
}

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_up() {
  require_kubectl
  require_helm
  check_logstash_secrets
  check_agent_values

  info "Creating namespace..."
  kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"

  info "Deploying Logstash..."
  kubectl apply -f "${SCRIPT_DIR}/logstash/"

  info "Adding Elastic Helm repo..."
  helm repo add elastic https://helm.elastic.co/ --force-update
  helm repo update

  info "Installing Elastic Agent ${AGENT_VERSION} via Helm (namespace: kube-system)..."
  helm upgrade --install elastic-agent elastic/elastic-agent \
    --version "${AGENT_VERSION}" \
    --namespace kube-system \
    --values "${SCRIPT_DIR}/elastic-agent/values.yaml"

  info ""
  info "Deployment applied. Watch rollout:"
  info "  kubectl rollout status deployment/logstash -n ${NAMESPACE}"
  info "  kubectl get pods -n ${NAMESPACE} -w"
  info "  kubectl get pods -n kube-system -l app=elastic-agent -w"
  info ""
  info "Logstash is exposed on your local machine at:"
  info "  Beats input : localhost:30044"
  info "  HTTP input  : localhost:30080"
  info "  Monitoring  : use 'kubectl port-forward -n ${NAMESPACE} deployment/logstash 9600:9600'"
}

cmd_down() {
  require_kubectl
  require_helm
  warn "This will DELETE Logstash (namespace: ${NAMESPACE}) and Elastic Agent (Helm release in kube-system)."
  read -r -p "Are you sure? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { info "Aborted."; exit 0; }

  helm uninstall elastic-agent --namespace kube-system --ignore-not-found 2>/dev/null || true
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found
  info "Done."
}

cmd_status() {
  require_kubectl
  require_helm

  echo ""
  echo "=== Logstash (namespace: ${NAMESPACE}) ==="
  kubectl get pods,services -n "${NAMESPACE}" -o wide 2>/dev/null || echo "(namespace not found)"

  echo ""
  echo "=== Elastic Agent (Helm) ==="
  helm status elastic-agent --namespace kube-system 2>/dev/null || echo "(not installed)"

  echo ""
  echo "=== Elastic Agent pods (kube-system) ==="
  kubectl get pods -n kube-system -l app=elastic-agent -o wide 2>/dev/null || true
}

# ── Entry point ───────────────────────────────────────────────────────────────

case "${1:-}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  *)
    echo "Usage: $0 {up|down|status}"
    exit 1
    ;;
esac
