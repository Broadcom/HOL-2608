#!/bin/bash
# fix-dnsmasq-resilience.sh
# Version: 2026-07-10c
#
# Idempotent prep script: hardens the dnsmasq K8s Deployment on the holorouter
# before golden template capture.
#
# Problem solved:
#   The original dnsmasq Deployment used RollingUpdate strategy + delete+recreate
#   in its management scripts. For a single-replica hostPort:53 workload, this
#   creates a guaranteed FailedScheduling race: the new pod cannot claim port 53
#   until the old pod's network sandbox is fully released, causing DNS outages of
#   30-90s during any config update — responsible for ~40-50% lab deployment failures.
#
# Changes applied (each step is idempotent):
#   1. ConfigMap: removes duplicate expand-hosts directive if present
#   2. Deployment: strategy=Recreate, priorityClassName=system-node-critical,
#      terminationGracePeriodSeconds=5, liveness+readiness TCP probes on port 53
#   3. update_configmap.py / remove_dns_entry.py: use rollout restart annotation
#      patch instead of delete+recreate
#
# Usage:
#   Run as root directly on the holorouter, or via SSH:
#     ssh root@router 'bash -s' < fix-dnsmasq-resilience.sh
#
# Requires: kubectl, python3 with kubernetes client (standard on holorouter)

set -euo pipefail

export KUBECONFIG=/etc/kubernetes/admin.conf
DNSMASQ_DIR="/holodeck-runtime/dnsmasq"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  [OK]${NC} $*"; }
info() { echo -e "${YELLOW}  [--]${NC} $*"; }
die()  { echo -e "${RED}  [FAIL]${NC} $*" >&2; exit 1; }

echo
echo "=== dnsmasq resilience fix ==="
echo

# ── Wait for K8s API ───────────────────────────────────────────────────────────
# When triggered from labstartup at router boot time, K8s may still be starting.

info "Waiting for K8s API (max 120s)..."
_kw=0
until kubectl get nodes --request-timeout=5s &>/dev/null; do
    [[ $_kw -ge 120 ]] && die "K8s API not ready after 120s"
    sleep 5; _kw=$((_kw + 5))
done
ok "K8s API ready (${_kw}s)"

# ── Preflight ──────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Must run as root"
[[ -d "$DNSMASQ_DIR" ]] || die "Expected $DNSMASQ_DIR not found - is this a holorouter?"
kubectl get deployment dnsmasq-deployment -n default &>/dev/null \
  || die "dnsmasq-deployment not found in K8s - is this a dnsmasq-based holorouter?"

ok "Preflight checks passed"

# ── Step 1: Fix duplicate expand-hosts in ConfigMap ───────────────────────────

info "Step 1: Checking ConfigMap for duplicate expand-hosts..."

COUNT=$(kubectl get configmap dnsmasq -n default \
  -o jsonpath='{.data.dnsmasq\.conf}' | grep -c '^expand-hosts$' || true)

if [[ "$COUNT" -gt 1 ]]; then
  info "Found $COUNT expand-hosts directives — removing duplicate..."
  python3 - <<'PYEOF'
import kubernetes.client, kubernetes.config
from kubernetes.client.rest import ApiException

kubernetes.config.load_kube_config()
api = kubernetes.client.CoreV1Api()
cm  = api.read_namespaced_config_map('dnsmasq', 'default')
conf = cm.data['dnsmasq.conf']
seen = False
fixed = []
for line in conf.splitlines():
    if line.strip() == 'expand-hosts':
        if not seen:
            seen = True
            fixed.append(line)
    else:
        fixed.append(line)
cm.data['dnsmasq.conf'] = '\n'.join(fixed)
api.patch_namespaced_config_map('dnsmasq', 'default', cm)
print("  ConfigMap patched")
PYEOF
  ok "Duplicate expand-hosts removed"
else
  ok "ConfigMap expand-hosts count OK ($COUNT)"
fi

# ── Step 2: Write updated management scripts ───────────────────────────────────

info "Step 2: Writing updated update_configmap.py..."
cat > "${DNSMASQ_DIR}/update_configmap.py" << 'PYEOF'
from __future__ import print_function
import sys
import time
from datetime import datetime, timezone
import kubernetes.client
import kubernetes.config
from kubernetes.client.rest import ApiException

kubernetes.config.load_kube_config()

api_instance   = kubernetes.client.CoreV1Api()
api_apps       = kubernetes.client.AppsV1Api()

name            = 'dnsmasq'
deployment_name = 'dnsmasq-deployment'
namespace       = 'default'
pretty          = 'true'
op_type         = sys.argv[1]

# --- 1. Read and mutate the ConfigMap ---
try:
    cm = api_instance.read_namespaced_config_map(name, namespace, pretty=pretty)
except ApiException as e:
    sys.exit(f'ERROR reading ConfigMap: {e}')

if op_type == 'create':
    dns_entry = sys.argv[2]
    if dns_entry in cm.data['hosts']:
        sys.exit('The DNS entry already exists in the DNS server')
    cm.data['hosts'] += '\n' + dns_entry

elif op_type == 'update':
    search_entry  = sys.argv[2]
    replace_entry = sys.argv[3]
    if search_entry not in cm.data['hosts']:
        sys.exit('The DNS entry does not exist in the DNS server')
    cm.data['hosts'] = cm.data['hosts'].replace(search_entry, replace_entry)

else:
    sys.exit(f'Unknown operation: {op_type}. Use create or update.')

# --- 2. Patch the ConfigMap ---
try:
    api_instance.patch_namespaced_config_map(name, namespace, cm, pretty=pretty)
    print(f'ConfigMap updated ({op_type})')
except ApiException as e:
    sys.exit(f'ERROR patching ConfigMap: {e}')

# --- 3. Trigger a rollout restart of the Deployment.
#    With strategy.type=Recreate this terminates the old pod first,
#    then starts a new pod that mounts the updated ConfigMap.
#    This replaces the previous delete+recreate approach which caused
#    a FailedScheduling race on hostPort 53 (old pod sandbox held the
#    port while the new pod was already being scheduled).
try:
    restart_patch = {
        'spec': {
            'template': {
                'metadata': {
                    'annotations': {
                        'kubectl.kubernetes.io/restartedAt': datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    api_apps.patch_namespaced_deployment(deployment_name, namespace, restart_patch)
    print('Rollout restart triggered (strategy: Recreate)')
    print('DNS Record successfully updated')
except ApiException as e:
    sys.exit(f'ERROR triggering rollout restart: {e}')
PYEOF
ok "update_configmap.py written"

info "Step 2: Writing updated remove_dns_entry.py..."
cat > "${DNSMASQ_DIR}/remove_dns_entry.py" << 'PYEOF'
from __future__ import print_function
import sys
from datetime import datetime, timezone
import kubernetes.client
import kubernetes.config
from kubernetes.client.rest import ApiException

kubernetes.config.load_kube_config()

api_instance   = kubernetes.client.CoreV1Api()
api_apps       = kubernetes.client.AppsV1Api()

name            = 'dnsmasq'
deployment_name = 'dnsmasq-deployment'
namespace       = 'default'
pretty          = 'true'

# --- 1. Read and mutate the ConfigMap ---
try:
    cm = api_instance.read_namespaced_config_map(name, namespace, pretty=pretty)
except ApiException as e:
    sys.exit(f'ERROR reading ConfigMap: {e}')

dns_entry = sys.argv[1]
if dns_entry not in cm.data['hosts']:
    sys.exit('The DNS entry does not exist in the DNS server')

cm.data['hosts'] = cm.data['hosts'].replace(dns_entry, '').strip()

# --- 2. Patch the ConfigMap ---
try:
    api_instance.patch_namespaced_config_map(name, namespace, cm, pretty=pretty)
    print('ConfigMap updated (remove)')
except ApiException as e:
    sys.exit(f'ERROR patching ConfigMap: {e}')

# --- 3. Trigger rollout restart (Recreate strategy - no hostPort race condition) ---
try:
    restart_patch = {
        'spec': {
            'template': {
                'metadata': {
                    'annotations': {
                        'kubectl.kubernetes.io/restartedAt': datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    api_apps.patch_namespaced_deployment(deployment_name, namespace, restart_patch)
    print('Rollout restart triggered (strategy: Recreate)')
    print('DNS Record successfully deleted')
except ApiException as e:
    sys.exit(f'ERROR triggering rollout restart: {e}')
PYEOF
ok "remove_dns_entry.py written"

# ── Step 3: Update Deployment (two-phase to avoid DNS gap during labstartup) ───
#
# Phase A — patch strategy only (non-disruptive, no pod restart).
#   Changing spec.strategy does NOT restart running pods; it only governs how
#   the *next* pod update is performed.
#
# Phase B — apply template changes (probes, priorityClass, grace period).
#   These are spec.template changes and DO trigger a pod restart (~15-20s DNS
#   gap).  We defer this until lsf.write_vpodprogress('READY') fires, which
#   writes /tmp/holorouter/ready onto the router.  By then labstartup is done
#   and a brief post-ready DNS hiccup is far less disruptive than a mid-startup
#   outage.  On subsequent runs the probes already exist so Phase B is skipped.

info "Step 3: Configuring dnsmasq Deployment..."

# Always write the target YAML to disk (used by Phase B apply).
cat > "${DNSMASQ_DIR}/dnsmasq_deployment.yaml" << 'YAMEOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dnsmasq-deployment
  labels:
    app: dnsmasq
spec:
  replicas: 1
  # Recreate (not RollingUpdate) is required for hostPort workloads:
  # RollingUpdate attempts to start the new pod before terminating the old one,
  # but since both need hostPort 53, the scheduler rejects the new pod with
  # FailedScheduling until the old pod's sandbox is fully released.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: dnsmasq
  template:
    metadata:
      namespace: dnsmasq
      labels:
        app: dnsmasq
    spec:
      hostNetwork: true
      # DNS is critical infrastructure - schedule before other workloads
      priorityClassName: system-node-critical
      # Minimize the DNS gap on restarts: terminate quickly once SIGTERM is sent
      terminationGracePeriodSeconds: 5
      containers:
      - name: dnsmasq
        image: vcf.lab/dnsmasq
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 53
          hostPort: 53
          name: dns-tcp
          protocol: TCP
        - containerPort: 53
          hostPort: 53
          name: dns-udp
          protocol: UDP
        - containerPort: 67
          hostPort: 67
          name: dhcp
          protocol: UDP
        securityContext:
          capabilities:
            drop: [all]
            add: [NET_ADMIN,NET_RAW,NET_BIND_SERVICE]
        resources:
          requests:
            cpu: 200m
            memory: 1Gi
          limits:
            cpu: "1"
            memory: 3Gi
        livenessProbe:
          tcpSocket:
            port: 53
          initialDelaySeconds: 10
          periodSeconds: 30
          failureThreshold: 3
          timeoutSeconds: 5
        readinessProbe:
          tcpSocket:
            port: 53
          initialDelaySeconds: 5
          periodSeconds: 10
          failureThreshold: 3
          timeoutSeconds: 3
        volumeMounts:
          - mountPath: '/etc/dnsmasq.conf'
            name: config-volume
            readOnly: true
            subPath: dnsmasq.conf
          - mountPath: '/etc/hosts'
            name: config-volume
            readOnly: true
            subPath: hosts
      volumes:
        - name: config-volume
          configMap:
            name: dnsmasq
YAMEOF

# ── Phase A: Patch strategy to Recreate (non-disruptive) ──────────────────────
CURRENT_STRATEGY=$(kubectl get deployment dnsmasq-deployment -n default \
  -o jsonpath='{.spec.strategy.type}')

if [[ "$CURRENT_STRATEGY" != "Recreate" ]]; then
  info "Phase A: Patching strategy to Recreate (no pod restart)..."
  kubectl patch deployment dnsmasq-deployment -n default \
    --type=merge -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'
  ok "Strategy patched to Recreate"
else
  ok "Phase A: Strategy already Recreate"
fi

# ── Phase B: Apply template changes (deferred until labstartup is READY) ──────
# Check whether probes are already present — if yes, skip entirely.
HAS_LIVENESS=$(kubectl get deployment dnsmasq-deployment -n default \
  -o jsonpath='{.spec.template.spec.containers[0].livenessProbe}' 2>/dev/null)

if [[ -z "$HAS_LIVENESS" ]]; then
  info "Phase B: Template changes needed (probes, priorityClass, grace period)."
  info "  Waiting for labstartup READY signal to avoid a DNS gap during startup..."
  info "  (/tmp/holorouter/ready — written by lsf.write_vpodprogress READY)"
  _w=0; _max=5400  # wait up to 90 min for long VCF startups
  until [[ -f /tmp/holorouter/ready ]] || [[ $_w -ge $_max ]]; do
    sleep 15; _w=$((_w + 15))
  done
  if [[ -f /tmp/holorouter/ready ]]; then
    ok "Phase B: Labstartup READY (${_w}s). Applying template changes..."
  else
    info "Phase B: READY timeout after ${_w}s — applying anyway"
  fi
  kubectl apply -f "${DNSMASQ_DIR}/dnsmasq_deployment.yaml"
  ok "Deployment YAML applied"

  # ── Step 4: Wait for pod Ready (only needed when pod actually restarts) ──────
  info "Step 4: Waiting for dnsmasq pod to be Ready (up to 60s)..."
  if kubectl rollout status deployment/dnsmasq-deployment -n default --timeout=60s; then
    ok "dnsmasq pod is Ready"
  else
    die "Pod did not become Ready within 60s — check: kubectl describe pod -l app=dnsmasq"
  fi
else
  ok "Phase B: Template already up to date — no pod restart needed"
  info "Step 4: Skipped (no pod restart required)"
fi

# ── Step 5: Verify DNS is responding ──────────────────────────────────────────

info "Step 5: Verifying DNS resolves a known host..."

RESOLVED=$(nslookup sddcmanager-a.site-a.vcf.lab 127.0.0.1 2>/dev/null \
  | awk '/^Address/ && !/127\.0\.0\.1/ {print $2; exit}')

if [[ -n "$RESOLVED" ]]; then
  ok "DNS resolution working: sddcmanager-a.site-a.vcf.lab → $RESOLVED"
else
  die "DNS resolution failed for sddcmanager-a.site-a.vcf.lab — check dnsmasq pod logs"
fi

echo
echo -e "${GREEN}=== All steps completed successfully ===${NC}"
echo "  Deployment strategy : $(kubectl get deployment dnsmasq-deployment -o jsonpath='{.spec.strategy.type}')"
echo "  Priority class      : $(kubectl get deployment dnsmasq-deployment -o jsonpath='{.spec.template.spec.priorityClassName}')"
echo "  Grace period        : $(kubectl get deployment dnsmasq-deployment -o jsonpath='{.spec.template.spec.terminationGracePeriodSeconds}')s"
echo "  Liveness probe      : TCP :53 (delay=10s, period=30s)"
echo "  Readiness probe     : TCP :53 (delay=5s, period=10s)"
echo
touch ~/fix-dnsmasq-reslience-ran.txt
